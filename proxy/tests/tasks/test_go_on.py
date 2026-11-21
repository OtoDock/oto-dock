"""Work goes on by itself after a restart or a machine's update.

A follow-up that waits behind a running turn is a run that has not started:
``pending`` until its round opens, kept by the stop and started again by the
boot. The lane gate also waits for a cut turn nobody settled and for an
earlier follow-up. A run whose turn its machine cut is left for the continue
pass and reopened. The harness is ``test_run_reopen``'s."""

from __future__ import annotations

import asyncio
import contextlib
import time
import uuid
from datetime import datetime, timedelta, timezone

import pytest

from core import background, drain
from core.events import stream_pump, turn_ending
from core.events.common_events import DONE, ERROR, TEXT, CommonEvent
from core.execution_layer import ResumeVerdict
from services.infra import platform_marks
from services.scheduler import (
    continue_pass,
    cut_turns,
    delivery,
    lanes,
    run_recovery,
    runner,
    scheduler,
    shared,
    stranded_runs,
    task_kinds,
)
from storage import database as task_store
from storage.automation import db_tasks, run_status
from storage.chat import cut_records
from tests.tasks.test_run_reopen import _boot, _pass
from tests.tasks.test_run_stream_terminal import _drive, _Layer, _settle

SID = "44444444-4444-4444-8444-444444444444"


@pytest.fixture(autouse=True)
def _a_boot_that_catches_up(monkeypatch):
    monkeypatch.setattr(platform_marks, "_boot", platform_marks.Boot(
        stamped=True, down_since=datetime.now(timezone.utc) - timedelta(minutes=5)))
    monkeypatch.setattr(run_recovery, "_REPORT_SPACING_S", 0.0)
    monkeypatch.setattr(lanes, "_LANE_POLL_S", 0.02)
    run_recovery._failed_at_boot.clear()
    shared._left_for_reopen.clear()
    cut_turns._held.clear()
    yield
    run_recovery._failed_at_boot.clear()
    shared._left_for_reopen.clear()
    cut_turns._held.clear()
    shared._shutting_down = False
    stream_pump._platform_ending = False


@pytest.fixture
def results(monkeypatch):
    """Every result a run hands its delegator, with how it names its lane."""
    got: list[dict] = []

    async def _deliver(task, final_status, output, **kw):
        got.append({"status": final_status, "output": output, **kw})
    monkeypatch.setattr(delivery, "_deliver_task_result", _deliver)
    monkeypatch.setattr(scheduler, "_deliver_task_result", _deliver)
    return got


def _lane_chat(agent: str = "agent-x") -> str:
    chat_id = f"lane-{uuid.uuid4().hex[:8]}"
    task_store.create_chat(chat_id, "user-1", agent, "default", model="m",
                           execution_path="claude-code-cli")
    task_store.update_chat(chat_id, session_id=SID)
    return chat_id


def _follow_up(task_id: str, chat_id: str) -> shared.TaskDefinition:
    """A delegated follow-up into ``chat_id``: its task row and its definition."""
    db_tasks.create_dynamic_task(task_id, "agent-x", "follow up", "now the tests", "cli", "one_time",
                                 None, None, 0, 600, None, scope="agent",
                                 notification_mode="none", continue_session=SID,
                                 target_chat_id=chat_id)
    shared._dynamic_task_ids.add(task_id)
    return _follow_up_definition(task_id, chat_id)


def _follow_up_definition(task_id: str, chat_id: str) -> shared.TaskDefinition:
    return shared.TaskDefinition(id=task_id, name="follow up", agent="agent-x",
                                 prompt="now the tests", scope="agent", notification_mode="none",
                                 delay_seconds=0, continue_session=SID, target_chat_id=chat_id,
                                 on_complete_agent="boss")


class _Gate:
    """The lane gate, held until ``open`` is set."""

    def __init__(self, monkeypatch, *, raises: Exception | None = None):
        self.entered = asyncio.Event()
        self.open = asyncio.Event()
        self.raises = raises

        async def _settle_prior_lane(chat_id, run_id):
            self.entered.set()
            await self.open.wait()
            if self.raises is not None:
                raise self.raises
            return False
        monkeypatch.setattr(lanes, "_settle_prior_lane", _settle_prior_lane)


class _ResumingLayer(_Layer):
    async def can_resume_session(self, *a, **k):
        return ResumeVerdict.RESUMABLE


class TestAFollowUpBehindARunningTurn:
    @pytest.mark.asyncio
    async def test_it_is_pending_at_the_gate_and_running_once_its_round_opens(
            self, temp_db, monkeypatch, results):
        chat_id = _lane_chat()
        gate = _Gate(monkeypatch)
        run_id = "run-fu1"
        t, q = _drive(monkeypatch, _ResumingLayer(hold=False), _follow_up("t-fu1", chat_id), run_id, SID)
        try:
            await asyncio.wait_for(gate.entered.wait(), 10)
            waiting = temp_db.get_run(run_id)
            assert waiting["status"] == run_status.PENDING
            assert not waiting.get("session_id") and not waiting.get("started_at")
            opened_after = datetime.now(timezone.utc).isoformat()
            gate.open.set()
            await asyncio.wait_for(q.first_text.wait(), 10)
            row = temp_db.get_run(run_id)
            assert row["status"] == run_status.RUNNING
            assert row["chat_id"] == chat_id and row["session_id"]
            # The start is the round's, not the wait's: the recovery reads the
            # run's first row from it.
            assert row["started_at"] >= opened_after
            await asyncio.wait_for(t, 10)
        finally:
            gate.open.set()
            await _settle(chat_id)
        assert [r["status"] for r in results] == [run_status.COMPLETED]
        assert results[0]["run_started_at"] >= opened_after

    @pytest.mark.asyncio
    async def test_the_stop_leaves_it_and_the_boot_starts_it_again(self, temp_db, monkeypatch, results):
        """The internal install's case: two follow-ups waited behind a
        lane's turn at a deploy. They came back as failed, with no chat."""
        chat_id = _lane_chat()
        gate = _Gate(monkeypatch)
        run_id = "run-fu2"
        t, _q = _drive(monkeypatch, _ResumingLayer(hold=False), _follow_up("t-fu2", chat_id), run_id, SID)
        await asyncio.wait_for(gate.entered.wait(), 10)
        # The graceful stop: the runs are cancelled.
        stream_pump.note_platform_ending()
        shared._shutting_down = True
        t.cancel()
        with pytest.raises(asyncio.CancelledError):
            await t
        assert temp_db.get_run(run_id)["status"] == run_status.PENDING
        assert run_id in run_recovery.give_up_at_shutdown()
        # The next process.
        _boot()
        assert run_recovery._failed_at_boot == []
        assert temp_db.get_run(run_id)["status"] == run_status.PENDING
        started: list[tuple[str, str, bool]] = []
        monkeypatch.setattr(runner, "reopen", lambda run, task, prompt, fresh=False:
                            started.append((run["id"], prompt, fresh)) or True)
        shared._active_task_ids.pop("t-fu2", None)
        assert await run_recovery.start_pending_runs() == 1
        assert started == [(run_id, "now the tests", True)]
        assert results == []        # nothing was reported: nothing failed

    @pytest.mark.asyncio
    async def test_a_boot_that_takes_nothing_up_reports_it_with_its_lane_and_the_reason(
            self, temp_db, monkeypatch, results):
        chat_id = _lane_chat()
        task_store.add_chat_messages_batch(chat_id, [("assistant", "the turn it waited behind", "", "")])
        _follow_up("t-fu3", chat_id)
        task_store.create_run("run-fu3", "t-fu3", "agent-x", "manual", None, "now the tests",
                              task_kinds.RUN_DELEGATE, "agent", None)
        cut_turns.record_at_boot(clean_stop=True, catches_up=False)
        run_recovery.defer_orphaned_runs(catches_up=False, clean_stop=True)
        await run_recovery.report_failed_at_boot()
        for task in background.running("run_recovery"):
            await task
        row = temp_db.get_run("run-fu3")
        assert row["status"] == run_status.FAILED
        assert row["error_message"] == run_recovery.RUN_NOT_STARTED
        assert len(results) == 1
        told = results[0]
        # It names the lane, and collects nothing of it: the rows are the
        # turn's it waited behind.
        assert told["lane_chat_id"] == chat_id and "worker_chat_id" not in told
        assert told["ending"].detail == turn_ending.NOT_STARTED
        assert "the turn it waited behind" not in told["output"]
        assert f'delegate(continue_id="{chat_id}")' in told["ending"].callback_note(told["lane_chat_id"])

    @pytest.mark.asyncio
    async def test_a_round_that_never_opened_reports_its_lane_and_collects_nothing(
            self, temp_db, monkeypatch, results):
        chat_id = _lane_chat()
        task_store.add_chat_messages_batch(chat_id, [("assistant", "an earlier round's report", "", "")])
        words = lanes.lane_words(lanes.LANE_MACHINE_AWAY, chat_id)
        gate = _Gate(monkeypatch, raises=lanes.LaneNotStarted(words))
        gate.open.set()
        t, _q = _drive(monkeypatch, _ResumingLayer(hold=False), _follow_up("t-fu4", chat_id), "run-fu4", SID)
        await asyncio.wait_for(t, 10)
        assert temp_db.get_run("run-fu4")["status"] == run_status.FAILED
        assert temp_db.get_run("run-fu4")["error_message"] == words
        assert len(results) == 1
        told = results[0]
        assert told["lane_chat_id"] == chat_id and "worker_chat_id" not in told
        # Nothing ran, so nothing failed: the result says "was not started",
        # and how to send it again, with the lane's chat.
        assert told["output"] == f'⚠ Delegated task "follow up" was not started: {words}'
        assert f'Send it again with delegate(continue_id="{chat_id}")' in told["output"]
        assert "failed" not in told["output"]
        assert "an earlier round's report" not in told["output"]


class TestAPlannedStopHoldsAFollowUp:
    """A follow-up released from the gate while a planned stop holds new work
    starts nothing: it stays ``pending`` for the drain's resume or the boot.
    With follow-ups run one after another, a drain would otherwise open a
    chain of new rounds."""

    @pytest.mark.asyncio
    async def test_it_stays_pending_keeps_its_task_row_and_starts_at_the_resume(
            self, temp_db, monkeypatch, results):
        chat_id = _lane_chat()
        gate = _Gate(monkeypatch)
        run_id, task_id = "run-h1", "t-h1"
        layer = _ResumingLayer(hold=False)
        started: list[str] = []
        real_start = layer.start_session

        async def _start(session_id, cfg):
            started.append(session_id)
            await real_start(session_id, cfg)
        layer.start_session = _start
        t, _q = _drive(monkeypatch, layer, _follow_up(task_id, chat_id), run_id, SID)
        try:
            await asyncio.wait_for(gate.entered.wait(), 10)
            assert run_id not in shared._open_rounds      # not open work for the stop
            await drain.drain(60)
            gate.open.set()                               # the turn it waited behind ended
            await asyncio.wait_for(t, 10)
            row = temp_db.get_run(run_id)
            assert row["status"] == run_status.PENDING and not row.get("session_id")
            assert started == [] and results == []
            assert task_store.get_dynamic_task(task_id) is not None
            # It keeps the task's reservation, as a fire held at its launch
            # does: another fire of the task during the hold gets this run
            # back and mints no second one.
            assert shared._active_task_ids.get(task_id) == run_id
            task = _follow_up_definition(task_id, chat_id)
            assert await runner._execute_task(task, trigger_type="manual") == run_id
            assert [r["id"] for r in task_store.list_orphaned_runs()] == [run_id]
            # The resume starts it, under its id, with its own prompt, and
            # takes the reservation for the start.
            relaunched: list[tuple] = []
            monkeypatch.setattr(runner, "reopen", lambda run, task, prompt, fresh=False:
                                relaunched.append((run["id"], prompt, fresh,
                                                   task.id in shared._active_task_ids)) or True)
            assert await run_recovery.start_pending_runs() == 0      # still held
            await drain.resume()
            assert await run_recovery.start_pending_runs() == 1
            assert relaunched == [(run_id, "now the tests", True, False)]
        finally:
            gate.open.set()
            drain.reset_for_tests()
            shared._active_task_ids.pop(task_id, None)
            await _settle(chat_id)

    @pytest.mark.asyncio
    async def test_a_round_opened_is_open_work_until_the_run_ends(self, temp_db, monkeypatch, results):
        chat_id = _lane_chat()
        gate = _Gate(monkeypatch)
        gate.open.set()
        layer = _ResumingLayer(hold=True)
        t, q = _drive(monkeypatch, layer, _follow_up("t-h2", chat_id), "run-h2", SID)
        try:
            await asyncio.wait_for(q.first_text.wait(), 10)
            assert "run-h2" in shared._open_rounds
            # A drain that starts now cuts nothing of an open round.
            await drain.drain(60)
            await layer.close_session(SID)
            await asyncio.wait_for(t, 10)
            assert temp_db.get_run("run-h2")["status"] == run_status.COMPLETED
            assert "run-h2" not in shared._open_rounds
        finally:
            drain.reset_for_tests()
            await _settle(chat_id)

    @pytest.mark.asyncio
    async def test_a_reopened_run_goes_on_under_a_hold(self, temp_db, monkeypatch):
        """A reopened run is a round already open: left at the gate it would
        stay ``running`` with nothing to start it again."""
        from services.scheduler import task_run
        task = _follow_up("t-h3", _lane_chat())
        run = task_run.TaskRun("run-h3", SID, task, "words", "manual", None, 1, None, None, "local",
                               reopened={"id": "run-h3", "started_at": "2026-01-01T00:00:00+00:00"})
        fresh = task_run.TaskRun("run-h4", SID, task, "p", "manual", None, 1, None, None, "local")
        try:
            await drain.drain(60)
            run._leave_if_held()
            with pytest.raises(task_run.LeftForReopen):
                fresh._leave_if_held()
        finally:
            drain.reset_for_tests()


def _delegate_run(run_id: str, task_id: str, chat_id: str, *, opened: bool) -> None:
    db_tasks.create_dynamic_task(task_id, "agent-x", "n", "p", "cli", "one_time", None, None, 0, 600,
                                 None, scope="agent", notification_mode="none",
                                 target_chat_id=chat_id)
    task_store.create_run(run_id, task_id, "agent-x", "manual", None, "p",
                          task_kinds.RUN_DELEGATE, "agent", None)
    if opened:
        task_store.update_run(run_id, status=run_status.RUNNING, chat_id=chat_id, session_id=SID,
                              started_at=datetime.now(timezone.utc).isoformat())


class TestTheLaneGate:
    def test_a_cut_turn_nobody_settled_holds_a_follow_up(self, temp_db):
        chat_id = _lane_chat()
        _delegate_run("run-g1", "t-g1", chat_id, opened=False)
        assert lanes._lane_waits(chat_id, "run-g1") == lanes.LaneWait()
        record = cut_records.add(chat_id, cause=cut_records.CAUSE_STOP, state=cut_records.WAITING,
                                 session_id=SID, kind="dashboard", machine_id="machine-1")
        away = lanes.lane_words(lanes.LANE_MACHINE_AWAY, chat_id)
        not_taken = lanes.lane_words(lanes.LANE_NOT_TAKEN_UP, chat_id)
        # Waiting for its machine: parked, nobody holds it.
        cut_turns._park(record, "tok")
        try:
            assert lanes._lane_waits(chat_id, "run-g1") == lanes.LaneWait(lanes.LANE_WAITS_CUT, away)
            # An adoption claimed it for its replay: held, until the record
            # settles. Its machine came back, so its words no longer say away.
            assert cut_turns.claim(SID) is not None
            assert lanes._lane_waits(chat_id, "run-g1") == lanes.LaneWait(
                lanes.LANE_WAITS_CUT, not_taken, held=True)
        finally:
            cut_turns._parked.pop(SID, None)
        # Its machine said the session is gone: the pass has not taken it yet.
        cut_records.make_pending(record["id"])
        assert lanes._lane_waits(chat_id, "run-g1") == lanes.LaneWait(lanes.LANE_WAITS_CUT, not_taken)
        # The pass holds it.
        holding = cut_turns.hold(record)
        assert cut_records.claim(record["id"]) is not None
        assert lanes._lane_waits(chat_id, "run-g1") == lanes.LaneWait(
            lanes.LANE_WAITS_CUT, not_taken, held=True)
        # A record whose holder is gone is held by nobody.
        cut_turns.let_go(holding)
        assert lanes._lane_waits(chat_id, "run-g1") == lanes.LaneWait(lanes.LANE_WAITS_CUT, not_taken)
        cut_records.settle(record["id"], cut_records.CONTINUED)
        assert lanes._lane_waits(chat_id, "run-g1") == lanes.LaneWait()

    def test_a_reopened_run_never_waits_on_its_own_record(self, temp_db):
        chat_id = _lane_chat()
        _delegate_run("run-g2", "t-g2", chat_id, opened=True)
        cut_records.add(chat_id, cause=cut_records.CAUSE_MACHINE, state=cut_records.PENDING,
                        session_id=SID, kind="task", run_id="run-g2")
        assert lanes._lane_waits(chat_id, "run-g2") == lanes.LaneWait()
        _delegate_run("run-g2b", "t-g2b", chat_id, opened=False)
        assert lanes._lane_waits(chat_id, "run-g2b").why == lanes.LANE_WAITS_CUT

    def test_follow_ups_run_in_the_order_they_were_sent(self, temp_db):
        chat_id = _lane_chat()
        # The run ids sort against the time: the order is the delegation's
        # own time, never the id's.
        first, second, third = "run-o3", "run-o2", "run-o1"
        for run_id in (first, second, third):
            _delegate_run(run_id, f"t-{run_id}", chat_id, opened=False)
            time.sleep(0.002)       # the task rows' own times
        assert [r["id"] for r in task_store.live_delegate_rounds(chat_id)] == [first, second, third]
        # Behind one that is still in line: its round has not opened.
        earlier = lanes.LaneWait(lanes.LANE_WAITS_EARLIER, behind=True)
        assert lanes._lane_waits(chat_id, first) == lanes.LaneWait()
        assert lanes._lane_waits(chat_id, second) == earlier
        assert lanes._lane_waits(chat_id, third) == earlier
        # The first one's round opened: it is the lane's running turn, and
        # the second is the next in line.
        task_store.update_run(first, status=run_status.RUNNING, chat_id=chat_id, session_id=SID)
        assert lanes._lane_waits(chat_id, second) == lanes.LaneWait(lanes.LANE_WAITS_EARLIER)
        assert lanes._lane_waits(chat_id, third) == earlier
        task_store.update_run(first, status=run_status.COMPLETED)
        assert lanes._lane_waits(chat_id, second) == lanes.LaneWait()
        assert lanes._lane_waits(chat_id, third) == earlier
        # A run that is no follow-up of the lane waits on no order.
        assert lanes._lane_waits(chat_id, "run-elsewhere") == lanes.LaneWait()

    @pytest.mark.asyncio
    async def test_the_gate_lets_a_follow_up_go_when_the_record_settles(self, temp_db):
        chat_id = _lane_chat()
        _delegate_run("run-g3", "t-g3", chat_id, opened=False)
        record = cut_records.add(chat_id, cause=cut_records.CAUSE_STOP, state=cut_records.WAITING,
                                 session_id=SID, kind="dashboard", machine_id="machine-1")
        waiter = asyncio.ensure_future(lanes._await_lane_turn(chat_id, "run-g3"))
        await asyncio.sleep(0.15)
        assert not waiter.done()
        cut_records.settle(record["id"], cut_records.LEFT)
        await asyncio.wait_for(waiter, 5)

    @pytest.mark.asyncio
    async def test_at_its_ceiling_a_follow_up_never_starts_beside_an_unsettled_turn(self, temp_db):
        chat_id = _lane_chat()
        _delegate_run("run-g4", "t-g4", chat_id, opened=False)
        record = cut_records.add(chat_id, cause=cut_records.CAUSE_STOP, state=cut_records.WAITING,
                                 session_id=SID, kind="dashboard", machine_id="machine-1")
        cut_turns._park(record, "tok")       # it waits for its machine
        try:
            with pytest.raises(lanes.LaneNotStarted) as refused:
                await lanes._await_lane_turn(chat_id, "run-g4", ceiling_seconds=0.1)
        finally:
            cut_turns._parked.pop(SID, None)
        assert str(refused.value) == lanes.lane_words(lanes.LANE_MACHINE_AWAY, chat_id)

    @staticmethod
    def _a_turn_runs(chat_id: str):
        """A turn's pump on the chat, as a replay or a continue registers it."""
        from types import SimpleNamespace
        pump = SimpleNamespace(is_done=False, session_id=SID)
        stream_pump._active_pumps[chat_id] = pump
        return pump

    @pytest.mark.asyncio
    async def test_a_follow_up_is_never_ended_behind_a_replayed_turn_that_runs(self, temp_db):
        """The review's first probe, and the walk's own log: the machine
        replays the lane's turn after a restart. Its record stays ``waiting``
        until the replayed turn ends, so the gate read "nobody took it" and
        ended the follow-up after 30 minutes with "The worker's machine has
        not come back", while the lane worked."""
        chat_id = _lane_chat()
        _delegate_run("run-g7", "t-g7", chat_id, opened=False)
        record = cut_records.add(chat_id, cause=cut_records.CAUSE_STOP, state=cut_records.WAITING,
                                 session_id=SID, kind="task", machine_id="machine-1")
        self._a_turn_runs(chat_id)
        try:
            await lanes._await_lane_turn(chat_id, "run-g7", ceiling_seconds=0.1)
        finally:
            stream_pump._active_pumps.pop(chat_id, None)
        assert cut_records.unsettled_of(chat_id)["id"] == record["id"]

    @pytest.mark.asyncio
    async def test_a_follow_up_is_never_ended_behind_a_continued_turn_that_runs(self, temp_db):
        """The review's second probe: the continue pass holds the record
        (``running``) until the continued turn ends. The gate read "has not
        been taken up again"."""
        chat_id = _lane_chat()
        _delegate_run("run-g8", "t-g8", chat_id, opened=False)
        record = cut_records.add(chat_id, cause=cut_records.CAUSE_MACHINE,
                                 state=cut_records.PENDING, session_id=SID, kind="dashboard")
        assert cut_records.claim(record["id"]) is not None
        self._a_turn_runs(chat_id)
        try:
            await lanes._await_lane_turn(chat_id, "run-g8", ceiling_seconds=0.1)
        finally:
            stream_pump._active_pumps.pop(chat_id, None)

    @pytest.mark.asyncio
    async def test_the_live_turn_wait_takes_over_from_the_gate(self, temp_db, monkeypatch):
        """Past the gate's ceiling a taken-back turn is waited out as any
        live turn of the lane is: ``_settle_prior_lane`` goes on to its
        quiescence wait, and the follow-up opens once the turn has ended."""
        chat_id = _lane_chat()
        _delegate_run("run-g9", "t-g9", chat_id, opened=False)
        cut_records.add(chat_id, cause=cut_records.CAUSE_STOP, state=cut_records.WAITING,
                        session_id=SID, kind="task", machine_id="machine-1")
        pump = self._a_turn_runs(chat_id)
        monkeypatch.setattr(lanes.config, "LANE_GATE_CEILING_S", 0.1)
        monkeypatch.setattr(lanes, "_lane_pump_wedged", lambda prior: False)
        waited: list[str] = []

        async def _quiescence(cid, **kw):
            waited.append(cid)
            pump.is_done = True          # the taken-back turn ended
        monkeypatch.setattr(lanes, "_await_lane_quiescence", _quiescence)
        try:
            assert await lanes._settle_prior_lane(chat_id, "run-g9") is False
        finally:
            stream_pump._active_pumps.pop(chat_id, None)
        assert waited == [chat_id]

    @pytest.mark.asyncio
    async def test_a_record_somebody_holds_with_no_turn_yet_is_waited_for_then_ended(self, temp_db):
        """The pass claimed the record and is starting the chat's session
        (admission can take minutes): no turn runs yet. The follow-up is not
        ended for that at the ceiling. It is at twice the ceiling: a record
        that stays held with no turn is stuck."""
        chat_id = _lane_chat()
        _delegate_run("run-g10", "t-g10", chat_id, opened=False)
        record = cut_records.add(chat_id, cause=cut_records.CAUSE_STOP, state=cut_records.PENDING,
                                 session_id=SID, kind="dashboard")
        cut_turns.hold(record)
        assert cut_records.claim(record["id"]) is not None
        waiter = asyncio.ensure_future(lanes._await_lane_turn(chat_id, "run-g10", ceiling_seconds=0.6))
        await asyncio.sleep(0.7)         # past the ceiling, well before twice it
        assert not waiter.done(), "ended at the ceiling although the pass holds the record"
        with pytest.raises(lanes.LaneNotStarted) as refused:
            await asyncio.wait_for(waiter, 5)
        assert str(refused.value) == lanes.lane_words(lanes.LANE_NOT_TAKEN_UP, chat_id)

    @pytest.mark.asyncio
    async def test_a_held_record_that_settles_lets_the_follow_up_go(self, temp_db):
        chat_id = _lane_chat()
        _delegate_run("run-g11", "t-g11", chat_id, opened=False)
        record = cut_records.add(chat_id, cause=cut_records.CAUSE_STOP, state=cut_records.PENDING,
                                 session_id=SID, kind="dashboard")
        cut_turns.hold(record)
        assert cut_records.claim(record["id"]) is not None
        waiter = asyncio.ensure_future(lanes._await_lane_turn(chat_id, "run-g11", ceiling_seconds=0.6))
        await asyncio.sleep(0.7)         # past the ceiling, well before twice it
        assert not waiter.done()
        cut_records.settle(record["id"], cut_records.CONTINUED)
        await asyncio.wait_for(waiter, 5)

    @pytest.mark.asyncio
    async def test_at_its_ceiling_an_earlier_follow_up_whose_round_is_open_is_passed(self, temp_db):
        """As a live pump is: the live-turn wait that follows takes over."""
        chat_id = _lane_chat()
        _delegate_run("run-g5", "t-g5", chat_id, opened=True)
        await asyncio.sleep(0.002)
        _delegate_run("run-g6", "t-g6", chat_id, opened=False)
        began = time.monotonic()
        await asyncio.wait_for(lanes._await_lane_turn(chat_id, "run-g6", ceiling_seconds=0.5), 5)
        assert time.monotonic() - began < 1.0, "waited for twice the ceiling"

    @pytest.mark.asyncio
    async def test_an_earlier_follow_up_still_in_line_is_passed_only_at_twice_the_ceiling(self, temp_db):
        """It waits for its slot, or is a moment before its round: the one
        behind it does not start first. One that stays in line with nothing
        on the lane is stuck, and is passed."""
        chat_id = _lane_chat()
        _delegate_run("run-g12", "t-g12", chat_id, opened=False)
        await asyncio.sleep(0.002)
        _delegate_run("run-g13", "t-g13", chat_id, opened=False)
        waiter = asyncio.ensure_future(lanes._await_lane_turn(chat_id, "run-g13", ceiling_seconds=0.6))
        await asyncio.sleep(0.7)         # past the ceiling, well before twice it
        assert not waiter.done(), "passed the follow-up ahead of it at the ceiling"
        await asyncio.wait_for(waiter, 5)

    @pytest.mark.asyncio
    async def test_two_follow_ups_never_leave_the_gate_for_one_turns_end(self, temp_db):
        """A taken-back turn runs past the gate's ceiling with two
        follow-ups behind it. The first goes to the live-turn wait. The
        second stays at the gate while the turn runs, and does not pass the
        first the moment the turn's record settles, nor while the first
        starts its round: both would find the lane quiet and start side by
        side. The second's ceiling counts again from the record's settle."""
        chat_id = _lane_chat()
        _delegate_run("run-f1", "t-f1", chat_id, opened=False)
        await asyncio.sleep(0.002)
        _delegate_run("run-f2", "t-f2", chat_id, opened=False)
        record = cut_records.add(chat_id, cause=cut_records.CAUSE_STOP, state=cut_records.WAITING,
                                 session_id=SID, kind="task", machine_id="machine-1")
        pump = self._a_turn_runs(chat_id)
        try:
            await lanes._await_lane_turn(chat_id, "run-f1", ceiling_seconds=0.1)
            second = asyncio.ensure_future(
                lanes._await_lane_turn(chat_id, "run-f2", ceiling_seconds=1.0))
            await asyncio.sleep(1.2)     # past the ceiling
            assert not second.done(), "left the gate beside the follow-up ahead of it"
            # The turn ends. The first is in its settle wait, a moment
            # before its round.
            cut_records.settle(record["id"], cut_records.CONTINUED)
            pump.is_done = True
            await asyncio.sleep(0.1)
            assert not second.done(), "passed the first follow-up when the record settled"
            # The first opens its round. Its session starts: no turn runs yet.
            task_store.update_run("run-f1", status=run_status.RUNNING, chat_id=chat_id,
                                  session_id=SID)
            await asyncio.sleep(0.1)
            assert not second.done(), "started beside the first follow-up's session start"
            task_store.update_run("run-f1", status=run_status.COMPLETED)
            await asyncio.wait_for(second, 5)
        finally:
            stream_pump._active_pumps.pop(chat_id, None)

    @pytest.mark.asyncio
    async def test_a_follow_up_stays_at_the_gate_while_the_one_ahead_starts_its_session(
            self, temp_db, monkeypatch):
        """No restart, no record. Two follow-ups behind one turn of the lane
        that runs past the ceiling. The turn ends and the first opens its
        round: its row has its session and no pump is up while the session
        starts. The second has waited past the ceiling as "earlier" the whole
        time. It left the gate at its next poll, found no pump, and started
        the lane's one session beside the first."""
        chat_id = _lane_chat()
        _delegate_run("run-q1", "t-q1", chat_id, opened=False)
        await asyncio.sleep(0.002)
        _delegate_run("run-q2", "t-q2", chat_id, opened=False)
        pump = self._a_turn_runs(chat_id)
        monkeypatch.setattr(lanes.config, "LANE_GATE_CEILING_S", 1.0)
        try:
            second = asyncio.ensure_future(lanes._settle_prior_lane(chat_id, "run-q2"))
            await asyncio.sleep(1.3)             # past the ceiling
            assert not second.done()
            pump.is_done = True                  # the turn they waited behind ended
            task_store.update_run("run-q1", status=run_status.RUNNING, chat_id=chat_id,
                                  session_id=SID)
            await asyncio.sleep(0.5)             # the first starts its session: no pump yet
            assert not second.done(), "the second left the gate beside the first's session start"
            task_store.update_run("run-q1", status=run_status.COMPLETED)
            assert await asyncio.wait_for(second, 5) is False
        finally:
            stream_pump._active_pumps.pop(chat_id, None)

    @pytest.mark.asyncio
    async def test_a_reason_that_changes_starts_the_ceiling_again(self, temp_db):
        """No turn ran on the lane: the pass held the record past the ceiling
        and then dropped it. The first follow-up opens its round. The second
        now waits for another reason, behind the first, and its ceiling
        counts from there: counted from the gate's start it left at once,
        beside the first's session start."""
        chat_id = _lane_chat()
        _delegate_run("run-q8", "t-q8", chat_id, opened=False)
        await asyncio.sleep(0.002)
        _delegate_run("run-q9", "t-q9", chat_id, opened=False)
        record = cut_records.add(chat_id, cause=cut_records.CAUSE_MACHINE, state=cut_records.PENDING,
                                 session_id=SID, kind="dashboard")
        cut_turns.hold(record)
        assert cut_records.claim(record["id"]) is not None
        second = asyncio.ensure_future(lanes._await_lane_turn(chat_id, "run-q9", ceiling_seconds=0.6))
        await asyncio.sleep(0.75)            # past the ceiling, the record held, no turn
        assert not second.done()
        cut_records.settle(record["id"], cut_records.DROPPED)
        task_store.update_run("run-q8", status=run_status.RUNNING, chat_id=chat_id, session_id=SID)
        await asyncio.sleep(0.3)             # the first starts its session
        assert not second.done(), "left the gate the moment the record settled"
        task_store.update_run("run-q8", status=run_status.COMPLETED)
        await asyncio.wait_for(second, 5)

    @pytest.mark.asyncio
    async def test_the_bound_at_twice_the_ceiling_counts_quiet_time_too(self, temp_db):
        """A short ceiling: the second has waited twice the ceiling while the
        turn still ran. At the turn's end the first is a moment before its
        round. The bound that passes a follow-up stuck in line counted the
        gate's own wait: the second started first, the first beside it."""
        chat_id = _lane_chat()
        _delegate_run("run-q6", "t-q6", chat_id, opened=False)
        await asyncio.sleep(0.002)
        _delegate_run("run-q7", "t-q7", chat_id, opened=False)
        pump = self._a_turn_runs(chat_id)
        try:
            second = asyncio.ensure_future(
                lanes._await_lane_turn(chat_id, "run-q7", ceiling_seconds=0.4))
            await asyncio.sleep(1.0)             # past twice the ceiling, the turn still runs
            assert not second.done()
            pump.is_done = True
            await asyncio.sleep(0.55)            # quiet for the ceiling, not for twice it
            assert not second.done(), "passed the follow-up ahead of it at the turn's end"
            await asyncio.wait_for(second, 5)    # one that stays in line with nothing on the lane
        finally:
            stream_pump._active_pumps.pop(chat_id, None)

    @pytest.mark.asyncio
    async def test_a_follow_up_is_not_ended_in_the_moment_after_a_turn_ends(self, temp_db):
        """A turn of the chat ran while a parked record waited for its
        machine. The follow-up is ended as not started only once the lane
        has been quiet for the ceiling, never because the gate's own wait is
        that old."""
        chat_id = _lane_chat()
        _delegate_run("run-q3", "t-q3", chat_id, opened=False)
        record = cut_records.add(chat_id, cause=cut_records.CAUSE_STOP, state=cut_records.WAITING,
                                 session_id=SID, kind="task", machine_id="machine-1")
        cut_turns._park(record, "tok")
        pump = self._a_turn_runs(chat_id)
        try:
            waiter = asyncio.ensure_future(
                lanes._await_lane_turn(chat_id, "run-q3", ceiling_seconds=0.6))
            await asyncio.sleep(0.3)
            pump.is_done = True                  # the turn ended before the ceiling
            await asyncio.sleep(0.4)             # the wait is past the ceiling, the quiet is not
            assert not waiter.done(), "ended as not started a moment after a turn ran"
            with pytest.raises(lanes.LaneNotStarted):
                await asyncio.wait_for(waiter, 5)
        finally:
            cut_turns._parked.pop(SID, None)
            stream_pump._active_pumps.pop(chat_id, None)

    @pytest.mark.asyncio
    async def test_a_pump_that_ended_is_no_turn_that_runs(self, temp_db):
        """A pump stays registered after its turn ended until the next one
        replaces it. It holds no follow-up at the gate: a parked record
        behind it ends the follow-up at the ceiling, as with no pump."""
        chat_id = _lane_chat()
        _delegate_run("run-q4", "t-q4", chat_id, opened=False)
        record = cut_records.add(chat_id, cause=cut_records.CAUSE_STOP, state=cut_records.WAITING,
                                 session_id=SID, kind="task", machine_id="machine-1")
        cut_turns._park(record, "tok")
        self._a_turn_runs(chat_id).is_done = True
        try:
            assert lanes._lane_turn_runs(chat_id) is False
            with pytest.raises(lanes.LaneNotStarted) as refused:
                await lanes._await_lane_turn(chat_id, "run-q4", ceiling_seconds=0.1)
            assert str(refused.value) == lanes.lane_words(lanes.LANE_MACHINE_AWAY, chat_id)
        finally:
            cut_turns._parked.pop(SID, None)
            stream_pump._active_pumps.pop(chat_id, None)

    @pytest.mark.asyncio
    async def test_a_claimed_record_that_never_gets_its_turn_reads_not_taken_up(self, temp_db):
        """An adoption claimed the record and no turn came for twice the
        ceiling. The machine did come back: the follow-up's sentence never
        says that it has not."""
        chat_id = _lane_chat()
        _delegate_run("run-q5", "t-q5", chat_id, opened=False)
        record = cut_records.add(chat_id, cause=cut_records.CAUSE_STOP, state=cut_records.WAITING,
                                 session_id=SID, kind="task", machine_id="machine-1")
        cut_turns._park(record, "tok")
        assert cut_turns.claim(SID) is not None
        with pytest.raises(lanes.LaneNotStarted) as refused:
            await lanes._await_lane_turn(chat_id, "run-q5", ceiling_seconds=0.1)
        assert str(refused.value) == lanes.lane_words(lanes.LANE_NOT_TAKEN_UP, chat_id)
        assert "has not come back" not in str(refused.value)

    @pytest.mark.asyncio
    async def test_a_store_that_cannot_be_read_holds_nothing(self, temp_db, monkeypatch):
        import psycopg

        def _down(chat_id, run_id):
            raise psycopg.OperationalError("down")  # the store is away
        monkeypatch.setattr(lanes, "_lane_waits", _down)
        await lanes._await_lane_turn("lane-x", "run-x")


class _SlowStart(_ResumingLayer):
    """A layer whose session start waits until ``go``: how many starts were
    entered, and how many were in flight at once."""

    def __init__(self):
        super().__init__(hold=False)
        self.go = asyncio.Event()
        self.entered = 0
        self.in_flight = 0
        self.peak = 0

    async def start_session(self, session_id, agent_cfg):
        self.entered += 1
        self.in_flight += 1
        self.peak = max(self.peak, self.in_flight)
        try:
            await self.go.wait()
        finally:
            self.in_flight -= 1


def _drive_follow_up(monkeypatch, layer, task: shared.TaskDefinition, run_id: str) -> asyncio.Task:
    """A delegated follow-up through the real ``_run_task`` (``_drive``, with
    the run's kind a delegated worker's, which the lane gate reads)."""
    t, _q = _drive(monkeypatch, layer, task, run_id, SID)
    with task_store.get_conn() as conn:
        conn.execute("UPDATE task_runs SET task_type=%s WHERE id=%s",
                     (task_kinds.RUN_DELEGATE, run_id))
        conn.commit()
    return t


class TestTwoFollowUpsBehindOneLongTurn:
    @pytest.mark.asyncio
    async def test_they_run_one_after_the_other_through_the_real_runner(
            self, temp_db, monkeypatch, results):
        """Both through ``runner._run_task`` and the real gate. Only the
        settle wait after a turn's end is a stand-in (0.2 s for 5 s). Before
        the gate counted quiet time, two starts of the lane's one session
        were in flight at once and both runs read ``running``."""
        chat_id = _lane_chat()
        from types import SimpleNamespace
        turn = SimpleNamespace(is_done=False, session_id=SID)
        stream_pump._active_pumps[chat_id] = turn
        monkeypatch.setattr(lanes.config, "LANE_GATE_CEILING_S", 1.0)
        monkeypatch.setattr(lanes, "_lane_pump_wedged", lambda prior: False)

        async def _quiescence(cid, **kw):
            while True:
                live = stream_pump._active_pumps.get(cid)
                if live is None or live.is_done:
                    break
                await asyncio.sleep(0.02)
            await asyncio.sleep(0.2)
        monkeypatch.setattr(lanes, "_await_lane_quiescence", _quiescence)
        layer = _SlowStart()
        t1 = _drive_follow_up(monkeypatch, layer, _follow_up("t-e1", chat_id), "run-e1")
        await asyncio.sleep(0.01)
        t2 = _drive_follow_up(monkeypatch, layer, _follow_up("t-e2", chat_id), "run-e2")
        try:
            await asyncio.sleep(1.3)             # past the gate's ceiling
            assert layer.entered == 0
            turn.is_done = True                  # the long turn ends
            for _ in range(150):
                if layer.entered:
                    break
                await asyncio.sleep(0.02)
            assert layer.entered == 1
            await asyncio.sleep(0.4)             # the first's session start takes a moment
            assert layer.entered == 1, "the second started beside the first's session start"
            assert temp_db.get_run("run-e2")["status"] == run_status.PENDING
            layer.go.set()
            await asyncio.wait_for(asyncio.gather(t1, t2), 30)
            assert (layer.entered, layer.peak) == (2, 1)
            assert [r["status"] for r in results] == [run_status.COMPLETED, run_status.COMPLETED]
        finally:
            layer.go.set()
            for t in (t1, t2):
                if not t.done():
                    t.cancel()
            await asyncio.gather(t1, t2, return_exceptions=True)
            with contextlib.suppress(Exception):
                await _settle(chat_id)
            stream_pump._active_pumps.pop(chat_id, None)
            for run_id, task_id in (("run-e1", "t-e1"), ("run-e2", "t-e2")):
                shared._active_task_ids.pop(task_id, None)
                shared._run_subscribers.pop(run_id, None)
                shared._run_event_buffer.pop(run_id, None)


def test_the_gates_ceiling_cannot_be_set_under_five_minutes():
    """Under that the rules for a lane with no turn end follow-ups whose
    lane is only slow to come back: the continue pass starts records a few
    seconds apart, and a session's start can take minutes."""
    import ast
    from pathlib import Path
    tree = ast.parse(Path(lanes.config.__file__).read_text(encoding="utf-8"))
    call = next(n for n in ast.walk(tree)
                if isinstance(n, ast.Call) and getattr(n.func, "id", "") == "_cfg_int"
                and n.args and getattr(n.args[0], "value", "") == "LANE_GATE_CEILING_S")
    floor = next(k.value.value for k in call.keywords if k.arg == "minimum")
    assert (call.args[1].value, floor) == (1800, 300)
    assert lanes.config.LANE_GATE_CEILING_S >= 300


class TestTheLaneRanAgain:
    def test_a_follow_up_that_only_waits_does_not_stop_the_cut_runs_reopen(self, temp_db):
        chat_id = _lane_chat()
        _delegate_run("run-cut", "t-cut", chat_id, opened=True)
        time.sleep(0.002)
        _delegate_run("run-waits", "t-waits", chat_id, opened=False)
        _run, ran_again, _c, _e = run_recovery._lane_state("run-cut", chat_id)
        assert ran_again is False
        # A round that opened on the lane since is the lane's next round.
        task_store.update_run("run-waits", status=run_status.RUNNING, chat_id=chat_id,
                              session_id="55555555-5555-4555-8555-555555555555",
                              started_at=datetime.now(timezone.utc).isoformat())
        _run, ran_again, _c, _e = run_recovery._lane_state("run-cut", chat_id)
        assert ran_again is True

    def test_a_run_whose_machine_already_lost_its_session_is_not_parked(self, temp_db):
        """Its record is the pass's: a deadline for the machine would fail
        the run while its reopened round runs."""
        from tests.remote.test_run_recovery import _mk_remote_run
        run_id, chat_id, sid = _mk_remote_run(temp_db)
        cut_records.add(chat_id, cause=cut_records.CAUSE_MACHINE, state=cut_records.PENDING,
                        session_id=sid, kind="task", run_id=run_id, machine_id="machine-1")
        try:
            parked, _failed = run_recovery.defer_orphaned_runs(catches_up=True, clean_stop=True)
            assert parked == 0 and sid not in run_recovery._parked
            assert task_store.get_run(run_id)["status"] == run_status.RUNNING
        finally:
            run_recovery._parked.clear()


class _LostLayer(_Layer):
    """A turn that says half, then its machine comes back without the
    session: the lost ending, as the remote turn gives it. The next turn
    (the reopened round) finishes."""

    def __init__(self):
        super().__init__(hold=True)
        self.prompts: list[str] = []
        self.lost = asyncio.Event()
        self.half = asyncio.Event()
        self.first = True

    async def can_resume_session(self, *a, **k):
        return ResumeVerdict.RESUMABLE

    async def send_message(self, session_id, prompt, **kw):
        self.prompts.append(prompt)
        if not self.first:
            yield CommonEvent(type=TEXT, data={"content": "and the rest."})
            yield CommonEvent(type=DONE, data={})
            return
        self.first = False
        yield CommonEvent(type=TEXT, data={"content": "Half the work. "})
        self.half.set()
        await self.lost.wait()
        ending = turn_ending.TurnEnding(reason=turn_ending.LOST, detail="the machine came back without it")
        yield CommonEvent(type=ERROR, data={"message": ending.line(), "ending": ending.as_dict()})


def _delegated(task_id: str) -> shared.TaskDefinition:
    db_tasks.create_dynamic_task(task_id, "agent-x", "count", "count to four", "cli", "one_time",
                                 None, None, 0, 600, None, scope="agent", notification_mode="none")
    shared._dynamic_task_ids.add(task_id)
    return shared.TaskDefinition(id=task_id, name="count", agent="agent-x", prompt="count to four",
                                 scope="agent", notification_mode="none", delay_seconds=0)


class TestARunItsMachineCut:
    @pytest.mark.asyncio
    async def test_it_is_left_running_reopened_by_the_pass_and_reports_once(
            self, temp_db, monkeypatch, results):
        """The machine came back without the run's session ("Update now"
        under a live lane). Before, the run failed with "The machine's stream
        was lost mid-turn" and its caller continued it by hand."""
        from services.remote import machine_sessions
        run_id, task_id = "run-m1", "t-m1"
        chat_id = f"task-{run_id}"
        layer = _LostLayer()
        t, _q = _drive(monkeypatch, layer, _delegated(task_id), run_id, SID)
        try:
            await asyncio.wait_for(layer.half.wait(), 10)
            # What ``machine_sessions._end_turn`` does: the pass takes the turn,
            # then the lost marker reaches the turn's reader.
            pump = stream_pump._active_pumps[chat_id]
            assert pump.run_id == run_id
            stream_pump.the_pass_takes_it(chat_id)
            layer.lost.set()
            await asyncio.wait_for(t, 10)
            assert pump.cut_by_machine is True
            # No verdict, no report, and the one-time task's row is kept.
            assert temp_db.get_run(run_id)["status"] == run_status.RUNNING
            assert results == []
            assert task_store.get_dynamic_task(task_id) is not None
            assert run_id not in shared._left_for_reopen      # read by the frame
            # No card in the chat: nothing failed.
            assert not [r for r in task_store.get_chat_messages(chat_id)
                        if '"turn_ended"' in (r.get("event_data") or "")]
            # The record names the run, and the pass reopens it.
            monkeypatch.setattr(continue_pass, "kick", lambda: None)
            assert await machine_sessions.machine_lost_session(
                chat_id, SID, "machine-1", run_id=run_id) is True
            record = cut_records.unsettled_of(chat_id)
            assert record["run_id"] == run_id and record["cause"] == cut_records.CAUSE_MACHINE
            assert await _pass() == 1
            await asyncio.wait_for(shared._running_tasks[run_id], 10)
        finally:
            layer.lost.set()
            await _settle(chat_id)
        assert layer.prompts[1].startswith("The machine this chat runs on restarted")
        row = temp_db.get_run(run_id)
        assert row["status"] == run_status.COMPLETED and not row.get("error_message")
        assert [r["status"] for r in results] == [run_status.COMPLETED]
        assert "Half the work." in results[0]["output"] or results[0].get("worker_chat_id") == chat_id
        assert cut_records.unsettled_of(chat_id) is None

    @pytest.mark.asyncio
    async def test_a_left_run_with_no_record_fails_with_one_report_that_names_the_machine(
            self, temp_db, monkeypatch, results):
        """Another turn's record stands on the chat: it will not reopen this
        run, and nothing else would ever end it."""
        from services.remote import machine_sessions
        from tests.remote.test_run_recovery import _mk_remote_run
        run_id, chat_id, sid = _mk_remote_run(temp_db)
        monkeypatch.setattr(task_store, "get_dynamic_task", lambda tid: {"id": tid} if tid else None)
        monkeypatch.setattr(scheduler, "_row_to_task", lambda row: row["id"])
        cut_records.add(chat_id, cause=cut_records.CAUSE_STOP, state=cut_records.PENDING,
                        session_id="another-session", kind="dashboard")
        monkeypatch.setattr(continue_pass, "kick", lambda: None)
        assert await machine_sessions.machine_lost_session(
            chat_id, sid, "machine-1", run_id=run_id) is False
        row = task_store.get_run(run_id)
        assert row["status"] == run_status.FAILED
        assert row["error_message"] == run_recovery.RUN_CUT_MACHINE
        assert len(results) == 1 and results[0]["ending"].detail == turn_ending.MACHINE
        assert results[0]["worker_chat_id"] == chat_id

    @pytest.mark.asyncio
    async def test_a_standing_record_that_names_the_run_keeps_it(self, temp_db, monkeypatch, results):
        from services.remote import machine_sessions
        from tests.remote.test_run_recovery import _mk_remote_run
        run_id, chat_id, sid = _mk_remote_run(temp_db)
        cut_records.add(chat_id, cause=cut_records.CAUSE_MACHINE, state=cut_records.PENDING,
                        session_id=sid, kind="task", run_id=run_id)
        assert await machine_sessions.machine_lost_session(
            chat_id, sid, "machine-1", run_id=run_id) is False
        assert task_store.get_run(run_id)["status"] == run_status.RUNNING and results == []

    @pytest.mark.asyncio
    async def test_the_pass_waits_for_the_cut_runs_own_task_before_it_reopens(self, temp_db):
        """The pass can reach the reopen before the old task released the
        task's reservation: ``runner.reopen`` would refuse, and the run fail."""
        released = asyncio.Event()

        async def _old_task():
            await released.wait()
        task = asyncio.ensure_future(_old_task())
        shared._running_tasks["run-w1"] = task
        task.add_done_callback(lambda _t: shared._running_tasks.pop("run-w1", None))
        waiter = asyncio.ensure_future(runner.unwound("run-w1"))
        await asyncio.sleep(0.05)
        assert not waiter.done()
        released.set()
        await asyncio.wait_for(waiter, 5)
        assert "run-w1" not in shared._running_tasks
        await runner.unwound("run-none")       # nothing to wait for

    def test_the_card_of_a_record_follows_what_cut_it(self):
        import json
        machine = json.loads(cut_turns._closing(cut_records.CAUSE_MACHINE)[1])
        assert "machine this chat runs on restarted" in machine["message"]
        assert "platform" not in machine["message"]
        platform = json.loads(cut_turns._closing(cut_records.CAUSE_STOP)[1])
        assert "platform restarted" in platform["message"]
        assert run_recovery.cut_reason({"cause": cut_records.CAUSE_MACHINE}) == run_recovery.RUN_CUT_MACHINE
        assert run_recovery.cut_reason({"cause": cut_records.CAUSE_CRASH}) == run_recovery.RUN_CUT


class TestAReplayedRunItsMachineCut:
    """The brief's own sequence: a restart re-adopts a lane's turn, then the
    machine updates. The replay's pump is the run's reader."""

    @pytest.mark.asyncio
    async def test_it_is_left_for_the_pass_and_not_read_as_completed(self, temp_db, results):
        from types import SimpleNamespace

        from tests.remote.test_run_recovery import _mk_remote_run
        run_id, chat_id, _sid = _mk_remote_run(temp_db)
        pump = SimpleNamespace(cut_by_machine=True,
                               last_ending=turn_ending.TurnEnding(reason=turn_ending.LOST))
        await run_recovery._finalize_run(run_id, chat_id, pump=pump)
        assert task_store.get_run(run_id)["status"] == run_status.RUNNING
        assert results == []

    @pytest.mark.asyncio
    async def test_a_replayed_turn_that_ended_typed_fails_the_run_with_it(
            self, temp_db, monkeypatch, results):
        from types import SimpleNamespace

        from tests.remote.test_run_recovery import _mk_remote_run
        run_id, chat_id, _sid = _mk_remote_run(temp_db)
        monkeypatch.setattr(task_store, "get_dynamic_task", lambda tid: {"id": tid} if tid else None)
        monkeypatch.setattr(scheduler, "_row_to_task", lambda row: row["id"])
        ending = turn_ending.TurnEnding(reason=turn_ending.EXITED, exit_code=1)
        await run_recovery._finalize_run(run_id, chat_id,
                                         pump=SimpleNamespace(cut_by_machine=False, last_ending=ending))
        row = task_store.get_run(run_id)
        assert row["status"] == run_status.FAILED and row["error_message"] == ending.summary()
        assert [r["status"] for r in results] == [run_status.FAILED]
        assert results[0]["ending"] == ending

    @pytest.mark.asyncio
    async def test_a_replayed_turn_that_ended_well_completes_the_run(self, temp_db, monkeypatch, results):
        from types import SimpleNamespace

        from tests.remote.test_run_recovery import _mk_remote_run
        run_id, chat_id, _sid = _mk_remote_run(temp_db)
        monkeypatch.setattr(task_store, "get_dynamic_task", lambda tid: {"id": tid} if tid else None)
        monkeypatch.setattr(scheduler, "_row_to_task", lambda row: row["id"])
        await run_recovery._finalize_run(run_id, chat_id,
                                         pump=SimpleNamespace(cut_by_machine=False, last_ending=None))
        assert task_store.get_run(run_id)["status"] == run_status.COMPLETED


class TestWhoATurnIsDrivenFor:
    """A turn the platform starts with no socket tells its pump who it runs
    for and what started it: a usage limit that cuts it reads both."""

    @pytest.mark.asyncio
    async def test_the_pump_of_a_driven_turn_names_its_person_and_its_source(self, temp_db):
        chat_id = _lane_chat()
        seen: list[tuple[str, str]] = []

        class _OneTurn(_Layer):
            async def send_message(self, session_id, prompt, **kw):
                pump = stream_pump._active_pumps[chat_id]
                seen.append((pump.driven_for, pump.driven_by))
                yield CommonEvent(type=TEXT, data={"content": "noted"})
                yield CommonEvent(type=DONE, data={})

        try:
            assert await lanes._run_echo_turn_pumped(
                _OneTurn(hold=False), SID, chat_id, "agent-x", "a result arrived",
                person="user-1", source="delegate_result") == ""
            # A caller that names no source (a review, a monitor) leaves both
            # empty on its own pump.
            assert await lanes._run_echo_turn_pumped(
                _OneTurn(hold=False), SID, chat_id, "agent-x", "a review") == ""
        finally:
            await _settle(chat_id)
        assert seen == [("user-1", "delegate_result"), ("", "")]

    @pytest.mark.asyncio
    async def test_the_persistent_rung_hands_the_source_on(self, temp_db, monkeypatch):
        from core.session import session_manager
        chat_id = _lane_chat()
        got: list[dict] = []

        async def _pumped(layer, sid, cid, agent, prompt, **kw):
            got.append(kw)
            return ""

        class _Alive:
            async def is_session_alive(self, sid):
                return True
        monkeypatch.setattr(delivery, "_run_echo_turn_pumped", _pumped)
        monkeypatch.setattr(session_manager, "layer_for_chat", lambda *a, **k: _Alive())
        assert await delivery._deliver_via_persistent(
            SID, "agent-x", "a result", user_sub=None, role="manager", chat_id=chat_id,
            driven_by="schedule_wake") == ""
        assert got == [{"person": "", "source": "schedule_wake"}]

    def test_the_rung_that_starts_a_session_hands_the_source_on_too(self):
        """The one-shot rung reaches the pump through two more functions:
        each passes what started the turn, or a wake that had to start its
        session would keep the limit's card."""
        import inspect

        from services.scheduler import wake_fresh, wake_session
        assert "driven_by=driven_by" in inspect.getsource(delivery._deliver_via_oneshot)
        spawn = inspect.getsource(wake_session._spawn_wake_session)
        assert spawn.count("source=driven_by") == 2
        assert "source=source" in inspect.getsource(wake_fresh.run_echo)


def _waiting_run(temp_db, *, state: str, cause: str = cut_records.CAUSE_STOP):
    """A live delegated run no task drives, and its chat's unsettled record
    that names it: parked for its replay (``waiting``) or for its reopen."""
    from tests.remote.test_run_recovery import _mk_remote_run
    run_id, chat_id, sid = _mk_remote_run(temp_db)
    record = cut_records.add(chat_id, cause=cause, state=state, session_id=sid, kind="task",
                             run_id=run_id, machine_id="machine-1", turn_token="tok-w")
    return run_id, chat_id, sid, record


class TestAStopOnARunThatWaits:
    """Stop means stop. A run that waits for its replay or its reopen has no
    task to cancel: before, the Stop answered ``not_running`` and the run was
    reopened."""

    @pytest.fixture
    def told(self, monkeypatch, results):
        monkeypatch.setattr(task_store, "get_dynamic_task",
                            lambda tid: {"id": tid, "name": "the lane"} if tid else None)
        monkeypatch.setattr(scheduler, "_row_to_task", lambda row: row["id"])
        return results

    @pytest.mark.asyncio
    async def test_it_ends_cancelled_its_record_is_dropped_and_nothing_reopens_it(
            self, temp_db, told):
        from core.session import session_state
        from storage.chat import open_turns
        run_id, chat_id, sid, record = _waiting_run(temp_db, state=cut_records.PENDING,
                                                    cause=cut_records.CAUSE_MACHINE)
        open_turns.open_turn(chat_id, "tok-w", sid)
        session_state._session_security[sid] = object()
        try:
            assert await scheduler.cancel_run(run_id) is True
            row = task_store.get_run(run_id)
            assert (row["status"], row["error_message"]) == (run_status.CANCELLED, "Interrupted by user")
            settled = cut_records.get(record["id"])
            assert (settled["state"], settled["reason"]) == (cut_records.DROPPED, "a person stopped its run")
            # The turn's row goes with no card: the Stop is its end.
            assert open_turns.get_open(chat_id) is None
            assert not [r for r in task_store.get_chat_messages(chat_id)
                        if "turn_ended" in (r.get("event_data") or "")]
            # Its session nothing holds loses its context: its machine ends
            # the process when it reports.
            assert sid not in session_state._session_security
            # The delegator hears a stopped run, with its lane.
            assert [(r["status"], r["worker_chat_id"]) for r in told] == [
                (run_status.USER_INTERRUPTED, chat_id)]
            assert "was stopped by the user" in told[0]["output"]
            # The pass has nothing to reopen.
            assert await _pass() == 0
            assert task_store.get_run(run_id)["status"] == run_status.CANCELLED
            # A second Stop finds nothing.
            assert await scheduler.cancel_run(run_id) is False
        finally:
            session_state._session_security.pop(sid, None)

    @pytest.mark.asyncio
    async def test_the_stop_forgets_the_session_before_it_reports(
            self, temp_db, told, monkeypatch):
        """The session may run on its machine still: the Stop forgets it
        (``forget_session``, which waits for the disk when its context names a
        machine) before the delegator hears the stop."""
        from core.session import session_state
        run_id, _chat_id, sid, _record = _waiting_run(temp_db, state=cut_records.PENDING,
                                                      cause=cut_records.CAUSE_MACHINE)
        session_state._session_security[sid] = object()
        order: list[str] = []

        real_forget = session_state.forget_session

        async def forget(session_id: str) -> None:
            await real_forget(session_id)       # waits when its context names a machine
            order.append("on disk" if sid not in session_state._session_security else "too early")

        real_report = run_recovery.report_stopped

        async def report(run) -> None:
            order.append("reported")
            await real_report(run)

        monkeypatch.setattr(session_state, "forget_session", forget)
        monkeypatch.setattr(run_recovery, "report_stopped", report)
        try:
            assert await stranded_runs.stop(run_id) is True
            assert order == ["on disk", "reported"]
        finally:
            session_state._session_security.pop(sid, None)

    @pytest.mark.asyncio
    async def test_a_start_of_the_id_in_flight_keeps_what_is_kept_under_it(self, temp_db, told):
        """A Stop of a waiting run whose session id a start is taking right now
        leaves the context alone: it is the start's."""
        from core.session import session_state
        run_id, _chat_id, sid, _record = _waiting_run(temp_db, state=cut_records.PENDING,
                                                      cause=cut_records.CAUSE_MACHINE)
        marker = object()
        session_state._session_security[sid] = marker
        session_state.mark_starting(sid, 30)
        try:
            assert await stranded_runs.stop(run_id) is True
            assert session_state._session_security.get(sid) is marker
        finally:
            session_state.clear_starting(sid)
            session_state._session_security.pop(sid, None)

    @pytest.mark.asyncio
    @pytest.mark.parametrize("case", ["the_terminals_record", "a_session_the_index_knows_as_a_persons"])
    async def test_a_persons_terminal_the_run_drove_is_never_closed(self, temp_db, told, case):
        """A follow-up that drove a person's live terminal has the terminal's
        session on its row (``TaskRun._open_round``), and the boot names the
        run on the terminal's record (``_ensure_record``). An ordinary Stop
        of such a run closes nothing of the terminal. The Stop of the run
        while it waits dropped the terminal's context when nothing held the
        terminal yet (after a restart, before its machine's sign-in): the
        machine then closed the person's terminal."""
        from core.session import session_state
        from tests.remote.test_run_recovery import _mk_remote_run
        run_id, chat_id, sid = _mk_remote_run(temp_db)
        if case == "the_terminals_record":
            record = cut_records.add(chat_id, cause=cut_records.CAUSE_STOP, state=cut_records.WAITING,
                                     session_id=sid, kind=cut_turns.KIND_TERMINAL,
                                     machine_id="machine-1", turn_token="tok-w")
            cut_records.name_run(record["id"], run_id)       # as the boot does
        else:
            # No turn of the terminal was open at the cut: the boot wrote
            # the run a record of its own, with the session on its row.
            record = cut_records.add(chat_id, cause=cut_records.CAUSE_STOP, state=cut_records.WAITING,
                                     session_id=sid, kind="task", run_id=run_id,
                                     machine_id="machine-1")
            session_state._sessions[sid] = {"created": True, "client_type": "dashboard"}
        context = object()
        session_state._session_security[sid] = context
        try:
            assert await scheduler.cancel_run(run_id) is True
            assert task_store.get_run(run_id)["status"] == run_status.CANCELLED
            assert cut_records.get(record["id"])["state"] == cut_records.DROPPED
            # The terminal keeps its context: its machine takes it back.
            assert session_state._session_security.get(sid) is context
            assert [r["status"] for r in told] == [run_status.USER_INTERRUPTED]
        finally:
            session_state._session_security.pop(sid, None)
            session_state._sessions.pop(sid, None)

    @pytest.mark.asyncio
    async def test_the_runs_own_task_session_loses_its_context(self, temp_db, told):
        """The index knows the session as a task's (a crash kept its entry)."""
        from core.session import session_state
        run_id, _chat_id, sid, _record = _waiting_run(temp_db, state=cut_records.WAITING)
        session_state._sessions[sid] = {"created": True, "is_task": True}
        session_state._session_security[sid] = object()
        try:
            assert await scheduler.cancel_run(run_id) is True
            assert sid not in session_state._session_security
        finally:
            session_state._session_security.pop(sid, None)
            session_state._sessions.pop(sid, None)

    @pytest.mark.asyncio
    async def test_a_run_parked_for_its_replay_is_no_longer_waited_for(self, temp_db, told):
        run_id, chat_id, sid, record = _waiting_run(temp_db, state=cut_records.WAITING)
        cut_turns._park(record, "tok-w")
        run_recovery._parked[sid] = {"run_id": run_id, "chat_id": chat_id, "machine_id": "machine-1",
                                     "agent": "pa", "deadline": time.monotonic() + 600}
        try:
            assert await scheduler.cancel_run(run_id) is True
            assert sid not in run_recovery._parked and not cut_turns.waiting(sid)
            assert task_store.get_run(run_id)["status"] == run_status.CANCELLED
        finally:
            run_recovery._parked.pop(sid, None)
            cut_turns._parked.pop(sid, None)

    @pytest.mark.asyncio
    async def test_a_session_a_layer_holds_is_closed_by_its_layer(self, temp_db, told, monkeypatch):
        from core.session import session_manager
        run_id, _chat_id, sid, _record = _waiting_run(temp_db, state=cut_records.WAITING)
        closed: list[str] = []

        class _Holds:
            async def close_session(self, session_id):
                closed.append(session_id)
        monkeypatch.setattr(session_manager, "find_layer_for_session",
                            lambda session_id: _Holds() if session_id == sid else None)
        assert await scheduler.cancel_run(run_id) is True
        assert closed == [sid]

    @pytest.mark.asyncio
    async def test_a_run_with_no_record_of_its_own_is_stopped_and_the_other_record_stays(
            self, temp_db, told):
        """A live run with no record of its own is stopped, and another run's
        record stays."""
        from tests.remote.test_run_recovery import _mk_remote_run
        run_id, chat_id, sid = _mk_remote_run(temp_db)
        other = cut_records.add(chat_id, cause=cut_records.CAUSE_STOP, state=cut_records.PENDING,
                                session_id=sid, kind="task", run_id="run-another")
        assert await scheduler.cancel_run(run_id) is True
        assert task_store.get_run(run_id)["status"] == run_status.CANCELLED
        assert cut_records.get(other["id"])["state"] == cut_records.PENDING
        assert await scheduler.cancel_run("run-unknown") is False
        assert [r["status"] for r in told] == [run_status.USER_INTERRUPTED]

    @pytest.mark.asyncio
    async def test_the_reopen_and_the_stop_take_the_record_in_one_statement(
            self, temp_db, told, monkeypatch):
        """One of them wins. A reopen that lost the record launches nothing
        and writes no card: the person stopped the run."""
        run_id, chat_id, _sid, record = _waiting_run(temp_db, state=cut_records.PENDING,
                                                     cause=cut_records.CAUSE_MACHINE)
        claimed = cut_records.claim(record["id"])
        launched: list[str] = []
        monkeypatch.setattr(runner, "reopen",
                            lambda run, task, prompt, fresh=False: launched.append(run["id"]) or True)
        # The Stop first.
        assert await stranded_runs.stop(run_id) is True
        assert await run_recovery.reopen_run(task_store.get_run(run_id), claimed) is None
        assert launched == []
        assert not [r for r in task_store.get_chat_messages(chat_id)
                    if "turn_ended" in (r.get("event_data") or "")]

    @pytest.mark.asyncio
    async def test_the_pass_writes_no_card_for_a_reopen_a_stop_won(self, temp_db, told, monkeypatch):
        """The pass had taken the record when the person pressed Stop. Its
        reopen finds the record settled: the chat gets no card that says the
        run could not be reopened. The person stopped it."""
        run_id, chat_id, _sid, record = _waiting_run(temp_db, state=cut_records.PENDING,
                                                     cause=cut_records.CAUSE_MACHINE)
        claimed = cut_records.claim(record["id"])
        launched: list[str] = []
        carded: list[str] = []
        monkeypatch.setattr(runner, "reopen",
                            lambda run, task, prompt, fresh=False: launched.append(run["id"]) or True)

        async def _card(record, reason, release):
            carded.append(reason)
        monkeypatch.setattr(continue_pass, "_card", _card)
        assert await stranded_runs.stop(run_id) is True
        verdict = continue_pass.Verdict(continue_pass.REOPEN, "its run is reopened",
                                        task_store.get_chat(chat_id), run=task_store.get_run(run_id))
        await continue_pass._act(claimed, verdict, lambda: None)
        assert (launched, carded) == ([], [])
        assert cut_records.get(record["id"])["state"] == cut_records.DROPPED

    @pytest.mark.asyncio
    async def test_a_stop_that_lost_the_record_cancels_the_reopened_runs_task(
            self, temp_db, told, monkeypatch):
        run_id, _chat_id, _sid, record = _waiting_run(temp_db, state=cut_records.PENDING,
                                                      cause=cut_records.CAUSE_MACHINE)
        claimed = cut_records.claim(record["id"])
        hold = asyncio.Event()

        async def _the_reopened_run():
            await hold.wait()

        def _reopen(run, task, prompt, fresh=False):
            task_ = asyncio.ensure_future(_the_reopened_run())
            shared._running_tasks[run["id"]] = task_
            task_.add_done_callback(lambda _t: shared._running_tasks.pop(run["id"], None))
            return True
        monkeypatch.setattr(runner, "reopen", _reopen)
        try:
            assert await run_recovery.reopen_run(task_store.get_run(run_id), claimed) is True
            assert cut_records.get(record["id"])["state"] == cut_records.CONTINUED
            # The Stop comes a moment later: it finds the record taken and
            # stops the run the ordinary way, through its task.
            assert await scheduler.cancel_run(run_id) is True
            assert run_id in shared._user_cancelled_runs
            task_ = shared._running_tasks.get(run_id)
            with pytest.raises(asyncio.CancelledError):
                await task_
        finally:
            hold.set()
            shared._user_cancelled_runs.discard(run_id)


def _reopenable(temp_db):
    """A run its machine cut, with its record the pass holds, its real task
    row (one-time, so the row goes with the run that ends it) and the
    builders a run meets before its slot."""
    run_id, chat_id, sid, record = _waiting_run(temp_db, state=cut_records.PENDING,
                                                cause=cut_records.CAUSE_MACHINE)
    db_tasks.create_dynamic_task("task-x", "pa", "the lane", "do it", "cli", "one_time", None, None,
                                 0, 600, None, scope="agent", notification_mode="none")
    shared._dynamic_task_ids.add("task-x")
    return run_id, chat_id, sid, cut_records.claim(record["id"])


async def _ended(run_id: str) -> dict:
    """The run's row once it is no longer live (a task cancelled before its
    first step is ended a moment after its task)."""
    for _ in range(200):
        row = task_store.get_run(run_id)
        if not run_status.is_live(row["status"]) and "task-x" not in shared._active_task_ids:
            return row
        await asyncio.sleep(0.02)
    return task_store.get_run(run_id)


@pytest.fixture
def before_the_slot(monkeypatch):
    """What a reopened run meets before its slot, with the real runner."""
    from core.config import task_config_builder
    from core.session import session_state
    monkeypatch.setattr(session_state, "_save_sessions", lambda: None)
    monkeypatch.setattr(task_config_builder, "resolve_task_identity",
                        lambda *a, **k: task_config_builder.TaskIdentity("", "manager", "agent", None))
    # No compute machine serves here, and the wait for one is its own
    # tests' (test_concurrency_compute): the run goes to its slot at
    # once, whatever an earlier test left of the box's condition.
    from core import concurrency

    async def _no_wait(_serves, **_kw) -> bool:
        return False
    monkeypatch.setattr(concurrency, "wait_for_compute", _no_wait)
    shared._active_task_ids.pop("task-x", None)
    yield
    shared._active_task_ids.pop("task-x", None)
    shared._dynamic_task_ids.discard("task-x")


@pytest.mark.usefixtures("before_the_slot")
class TestAStopOnAReopenedRunBeforeItsRound:
    """A reopened run is ``running`` before its slot. A Stop there answered
    ``cancelled`` and the run stayed ``running``: the end of a run no slot
    admitted ended only a ``pending`` row. With the real ``reopen_run``,
    ``runner.reopen`` and ``_run_task``."""

    def _stopped(self, run_id: str, chat_id: str, row: dict, results: list[dict]) -> None:
        assert (row["status"], row["error_message"]) == (run_status.CANCELLED, "Interrupted by user")
        # One result, a stopped run's, with its lane.
        assert [(r["status"], r["worker_chat_id"]) for r in results] == [
            (run_status.USER_INTERRUPTED, chat_id)]
        assert "was stopped by the user" in results[0]["output"]
        # Nothing of the run is left: the task can fire again, and a second
        # Stop of this run is told so.
        assert "task-x" not in shared._active_task_ids
        assert run_id not in shared._user_cancelled_runs and run_id not in shared._running_tasks
        # A one-time task's row goes with the run that ended it, as after an
        # ordinary Stop.
        assert task_store.get_dynamic_task("task-x") is None

    @pytest.mark.asyncio
    @pytest.mark.parametrize("steps", [0, 1], ids=["before_its_first_step", "in_its_first_store_read"])
    async def test_a_stop_right_after_the_reopen_ends_the_run(self, temp_db, results, steps):
        """The review's probe in both orders. Before its first step the
        task's coroutine never starts, so nothing of the frame runs: the row
        stayed ``running``, the reservation stayed for ever, and the next
        boot reopened the run the person stopped."""
        run_id, chat_id, sid, claimed = _reopenable(temp_db)
        from core.session import session_state
        try:
            assert await run_recovery.reopen_run(task_store.get_run(run_id), claimed) is True
            task_ = shared._running_tasks[run_id]
            for _ in range(steps):
                await asyncio.sleep(0)
            assert await scheduler.cancel_run(run_id) is True      # the API answers "cancelled"
            await asyncio.gather(task_, return_exceptions=True)
            self._stopped(run_id, chat_id, await _ended(run_id), results)
            assert await scheduler.cancel_run(run_id) is False
        finally:
            shared._user_cancelled_runs.discard(run_id)
            session_state._sessions.pop(sid, None)

    @pytest.mark.asyncio
    async def test_a_stop_while_the_reopened_run_waits_for_its_slot_ends_it(
            self, temp_db, monkeypatch, results):
        """A full box: the reopened run waits for memory, ``running`` all the
        while."""
        from contextlib import asynccontextmanager

        from core import concurrency
        from core.session import session_state
        waiting = asyncio.Event()

        @asynccontextmanager
        async def _full_box(session_id, target="", execution_path=None, ring_key=""):
            waiting.set()
            await asyncio.Event().wait()
            yield
        monkeypatch.setattr(concurrency, "task_slot", _full_box)
        run_id, chat_id, sid, claimed = _reopenable(temp_db)
        try:
            assert await run_recovery.reopen_run(task_store.get_run(run_id), claimed) is True
            task_ = shared._running_tasks[run_id]
            await asyncio.wait_for(waiting.wait(), 10)
            assert temp_db.get_run(run_id)["status"] == run_status.RUNNING
            assert await scheduler.cancel_run(run_id) is True
            await asyncio.gather(task_, return_exceptions=True)
            self._stopped(run_id, chat_id, await _ended(run_id), results)
        finally:
            shared._user_cancelled_runs.discard(run_id)
            session_state._sessions.pop(sid, None)

    @pytest.mark.asyncio
    async def test_a_stop_that_loses_the_record_to_the_reopen_stops_the_reopened_run(
            self, temp_db, monkeypatch, results):
        """The Stop read the record, then the pass's reopen settled it and
        launched the run. The Stop's own settle loses. It finds the reopened
        run's task and cancels it, and the run ends as stopped."""
        from core import concurrency
        from core.session import session_state
        run_id, chat_id, sid, claimed = _reopenable(temp_db)
        at_its_settle, go = asyncio.Event(), asyncio.Event()
        settle = cut_turns.stopped_by_person

        async def _held(record):
            at_its_settle.set()
            await go.wait()
            return await settle(record)
        monkeypatch.setattr(cut_turns, "stopped_by_person", _held)

        # The reopened run stays at its slot's door until the Stop reaches
        # it. Past the door the run's own handler ends it, and that order is
        # test_a_run_whose_round_is_open_is_its_own_handlers'.
        @contextlib.asynccontextmanager
        async def _closed_door(*_a, **_kw):
            await asyncio.Event().wait()
            yield
        monkeypatch.setattr(concurrency, "task_slot", _closed_door)
        try:
            assert run_id not in shared._running_tasks
            stop = asyncio.ensure_future(scheduler.cancel_run(run_id))
            await asyncio.wait_for(at_its_settle.wait(), 10)
            assert await run_recovery.reopen_run(task_store.get_run(run_id), claimed) is True
            task_ = shared._running_tasks[run_id]
            go.set()
            assert await asyncio.wait_for(stop, 10) is True
            await asyncio.gather(task_, return_exceptions=True)
            assert cut_records.get(claimed["id"])["state"] == cut_records.CONTINUED
            self._stopped(run_id, chat_id, await _ended(run_id), results)
        finally:
            go.set()
            shared._user_cancelled_runs.discard(run_id)
            session_state._sessions.pop(sid, None)

    @pytest.mark.asyncio
    async def test_a_run_whose_round_is_open_is_its_own_handlers(self, temp_db, results):
        """The frame's end touches only a run no round of this process
        opened: an open round is ended by the run's own cancel handler."""
        run_id, _chat_id, _sid, _claimed = _reopenable(temp_db)
        shared._user_cancelled_runs.add(run_id)
        shared._open_rounds.add(run_id)
        try:
            await runner._end_unstamped(run_id)
            assert temp_db.get_run(run_id)["status"] == run_status.RUNNING and results == []
        finally:
            shared._user_cancelled_runs.discard(run_id)
            shared._open_rounds.discard(run_id)

    @pytest.mark.asyncio
    async def test_a_shutdowns_cancel_leaves_a_reopened_run_for_the_boot(
            self, temp_db, monkeypatch, results):
        """No person stopped it: the run stays ``running`` with its task row."""
        from core.session import session_state
        run_id, _chat_id, sid, claimed = _reopenable(temp_db)
        monkeypatch.setattr(shared, "_shutting_down", True)
        try:
            assert await run_recovery.reopen_run(task_store.get_run(run_id), claimed) is True
            task_ = shared._running_tasks[run_id]
            await asyncio.sleep(0)
            task_.cancel()
            await asyncio.gather(task_, return_exceptions=True)
            await asyncio.sleep(0.1)
            assert temp_db.get_run(run_id)["status"] == run_status.RUNNING
            assert task_store.get_dynamic_task("task-x") is not None
            assert results == []
        finally:
            session_state._sessions.pop(sid, None)


@pytest.mark.usefixtures("before_the_slot")
class TestAReopenedRunThatFailsBeforeItsSlot:
    """A reopened run is ``running`` before its slot. A failure there, outside
    the run's body, left it ``running`` until the next boot, with its
    one-time task row deleted, and its delegator heard nothing: the end of a
    run no slot admitted failed only a ``pending`` row. With the real
    ``reopen_run``, ``runner.reopen`` and ``_run_task``."""

    def _failed(self, run_id: str, chat_id: str, row: dict, results: list[dict], why: str) -> None:
        assert (row["status"], row["error_message"]) == (run_status.FAILED, why)
        assert row["completed_at"]
        # One result, a failed run's, with its lane and the reason.
        assert [(r["status"], r["worker_chat_id"]) for r in results] == [(run_status.FAILED, chat_id)]
        assert results[0]["output"] == f'⚠ Delegated task "the lane" failed: {why}'
        # Nothing of the run is left: the task can fire again.
        assert "task-x" not in shared._active_task_ids and run_id not in shared._running_tasks
        assert run_id not in shared._platform_interrupts
        assert task_store.get_dynamic_task("task-x") is None

    @pytest.mark.asyncio
    @pytest.mark.parametrize("where", ["its_slot_target", "the_wait_for_a_compute_machine"])
    async def test_it_ends_failed_and_its_delegator_is_told_once(self, temp_db, monkeypatch, results, where):
        from core import concurrency
        from core.session import session_state

        async def _raises(*_a, **_kw):
            raise RuntimeError("the store went away")
        if where == "its_slot_target":
            monkeypatch.setattr(runner, "_slot_target_of", _raises)
        else:
            # The real ``_slot_target_of``: no compute machine serves, and
            # its wait for one raises.
            monkeypatch.setattr(concurrency, "wait_for_compute", _raises)
        run_id, chat_id, sid, claimed = _reopenable(temp_db)
        try:
            assert await run_recovery.reopen_run(task_store.get_run(run_id), claimed) is True
            await asyncio.gather(shared._running_tasks[run_id], return_exceptions=True)
            self._failed(run_id, chat_id, await _ended(run_id), results, "the store went away")
        finally:
            session_state._sessions.pop(sid, None)

    @pytest.mark.asyncio
    @pytest.mark.parametrize("steps", [0, 1], ids=["before_its_first_step", "in_its_first_store_read"])
    async def test_the_platforms_own_stop_there_ends_it_with_its_reason(self, temp_db, results, steps):
        """A stall's reap or an offboarding cancels the run with a reason
        (``scheduler.platform_cancel_run``): failed with it, as a run that
        waited ``pending`` is."""
        from core.session import session_state
        run_id, chat_id, sid, claimed = _reopenable(temp_db)
        try:
            assert await run_recovery.reopen_run(task_store.get_run(run_id), claimed) is True
            task_ = shared._running_tasks[run_id]
            for _ in range(steps):
                await asyncio.sleep(0)
            assert scheduler.platform_cancel_run(run_id, "Its agent was removed") is True
            await asyncio.gather(task_, return_exceptions=True)
            self._failed(run_id, chat_id, await _ended(run_id), results, "Its agent was removed")
        finally:
            shared._platform_interrupts.pop(run_id, None)
            session_state._sessions.pop(sid, None)

    @pytest.mark.asyncio
    async def test_a_persons_stop_comes_before_the_platforms_reason(self, temp_db, results):
        """Both marks on one run: it ends as stopped, in the order the end of
        a run that waited ``pending`` reads them."""
        run_id, chat_id, _sid, _claimed = _reopenable(temp_db)
        shared._user_cancelled_runs.add(run_id)
        shared._platform_interrupts[run_id] = "a wait timed out"
        try:
            await runner._end_unstamped(run_id)
            row = temp_db.get_run(run_id)
            assert (row["status"], row["error_message"]) == (run_status.CANCELLED, "Interrupted by user")
            assert [(r["status"], r["worker_chat_id"]) for r in results] == [(run_status.USER_INTERRUPTED, chat_id)]
        finally:
            shared._user_cancelled_runs.discard(run_id)
            shared._platform_interrupts.pop(run_id, None)

    @pytest.mark.asyncio
    async def test_a_failure_of_a_run_whose_round_is_open_is_its_own_handlers(self, temp_db, results):
        run_id, _chat_id, _sid, _claimed = _reopenable(temp_db)
        shared._open_rounds.add(run_id)
        try:
            await runner._end_unstamped(run_id, error="after its round")
            assert temp_db.get_run(run_id)["status"] == run_status.RUNNING and results == []
        finally:
            shared._open_rounds.discard(run_id)


@pytest.fixture(scope="module")
def a_condition_left_on_a_closed_loop():
    """What a test that initialised the box leaves when its loop closes: the
    box's condition, bound to that loop. Made before the test's own start,
    as an earlier test of the same process would leave it."""
    from core import concurrency
    loop = asyncio.new_event_loop()

    async def _init_and_wait() -> None:
        concurrency.init()
        async with concurrency._cond:
            with contextlib.suppress(asyncio.TimeoutError):
                await asyncio.wait_for(concurrency._cond.wait(), 0.01)
    loop.run_until_complete(_init_and_wait())
    loop.close()


class TestARunAfterATestThatInitialisedTheBox:
    @pytest.mark.asyncio
    async def test_a_reopened_run_with_no_compute_machine_ends_with_the_sentence(
            self, temp_db, monkeypatch, results, a_condition_left_on_a_closed_loop):
        """A run of the platform's place waits for a compute machine on the
        box's condition. One an earlier test bound to its own loop made the
        wait raise, and the run stayed ``running``. With the real
        ``reopen_run``, ``runner.reopen`` and ``_run_task``, and the real
        wait."""
        from core import concurrency, placement
        from core.config import task_config_builder
        from core.session import session_state
        from tests._compute import serve_none
        assert concurrency._cond is None, "a test starts with no condition of another test's loop"
        monkeypatch.setattr(session_state, "_save_sessions", lambda: None)
        monkeypatch.setattr(task_config_builder, "resolve_task_identity",
                            lambda *a, **k: task_config_builder.TaskIdentity("", "manager", "agent", None))
        serve_none(monkeypatch)
        shared._active_task_ids.pop("task-x", None)
        run_id, _chat_id, sid, claimed = _reopenable(temp_db)
        try:
            assert await run_recovery.reopen_run(task_store.get_run(run_id), claimed) is True
            await asyncio.gather(shared._running_tasks[run_id], return_exceptions=True)
            row = await _ended(run_id)
            assert row["status"] == run_status.FAILED
            assert placement.NO_COMPUTE_SENTENCE in (row["error_message"] or "")
            assert [r["status"] for r in results] == [run_status.FAILED]
        finally:
            shared._active_task_ids.pop("task-x", None)
            shared._dynamic_task_ids.discard("task-x")
            session_state._sessions.pop(sid, None)


class TestADroppedRecordThatNamesARun:
    @pytest.mark.asyncio
    async def test_the_run_fails_with_its_one_report(self, temp_db, monkeypatch, results):
        """A record the pass drops with no card (a kind with no resume, a
        chat that is gone) left its run live with nothing to end it."""
        monkeypatch.setattr(task_store, "get_dynamic_task", lambda tid: {"id": tid} if tid else None)
        monkeypatch.setattr(scheduler, "_row_to_task", lambda row: row["id"])
        from tests.remote.test_run_recovery import _mk_remote_run
        run_id, chat_id, sid = _mk_remote_run(temp_db)
        record = cut_records.add(chat_id, cause=cut_records.CAUSE_MACHINE, state=cut_records.PENDING,
                                 session_id=sid, kind="meeting", run_id=run_id)
        assert await _pass() == 1
        assert cut_records.get(record["id"])["state"] == cut_records.DROPPED
        row = task_store.get_run(run_id)
        assert (row["status"], row["error_message"]) == (run_status.FAILED, run_recovery.RUN_CUT_MACHINE)
        assert [r["status"] for r in results] == [run_status.FAILED]
        assert not [r for r in task_store.get_chat_messages(chat_id)
                    if "turn_ended" in (r.get("event_data") or "")]
