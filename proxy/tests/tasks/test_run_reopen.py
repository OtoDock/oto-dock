"""A run the platform cut is reopened under its own id, and its delegator
gets one result (``run_recovery.reopen_run``, ``runner.reopen``,
``TaskRun(reopened=)``).

The harness is ``test_run_stream_terminal``'s: the real ``_run_task`` on
``temp_db`` with a fake layer and the builders patched. The stop is the real
one's steps in its order (the platform's ending noted, the run cancelled, the
session closed), and the boot is the real boot's (the record, the kept run,
the pass)."""

from __future__ import annotations

import asyncio
import json
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

import pytest

from core import background, drain
from core.events import chat_writer, stream_pump, turn_ending
from core.events.common_events import (
    DONE,
    ERROR,
    TEXT,
    TOOL_RESULT,
    TOOL_USE,
    CommonEvent,
)
from core.execution_layer import ResumeVerdict
from services.infra import platform_marks
from services.scheduler import (
    continue_pass,
    cut_turns,
    delivery,
    run_recovery,
    runner,
    scheduler,
    shared,
    task_kinds,
)
from storage import database as task_store
from storage.automation import db_tasks, run_status
from storage.chat import cut_records, open_turns
from tests.tasks.test_run_stream_terminal import _drive, _Layer, _settle
from ws import wire_events as wire

SID = "33333333-3333-4333-8333-333333333333"


class _CutLayer(_Layer):
    """A turn that says half, then waits. Closed under the platform's stop
    its engine exits (the typed ending a killed CLI gives). After the
    restart the next turn finishes."""

    def __init__(self):
        super().__init__(hold=True)
        self.prompts: list[str] = []
        self.cut = True
        self.resume = ResumeVerdict.RESUMABLE
        self.half = asyncio.Event()

    async def send_message(self, session_id, prompt, **kw):
        self.prompts.append(prompt)
        if not self.cut:
            yield CommonEvent(type=TEXT, data={"content": "and the rest."})
            yield CommonEvent(type=DONE, data={})
            return
        yield CommonEvent(type=TEXT, data={"content": "Half the work. "})
        self.half.set()
        await self._closed.wait()
        exited = turn_ending.TurnEnding(reason=turn_ending.EXITED, exit_code=143)
        yield CommonEvent(type=ERROR, data={"message": "exited", "ending": exited.as_dict()})

    async def can_resume_session(self, *a, **k):
        return self.resume


class _ToolsLayer(_CutLayer):
    """A call that answered and a call open at the stop, as Direct LLM gives
    them to the pump: its tool's end carries a preview and no result."""

    async def send_message(self, session_id, prompt, **kw):
        yield CommonEvent(type=TOOL_USE, data={"name": "Write", "tool_id": "t-1"})
        yield CommonEvent(type=TOOL_RESULT, data={"name": "", "tool_id": "t-1", "result_preview": "ok"})
        yield CommonEvent(type=TOOL_USE, data={"name": "mcp__mail__send", "tool_id": "t-2"})
        self.half.set()
        await self._closed.wait()
        exited = turn_ending.TurnEnding(reason=turn_ending.EXITED, exit_code=143)
        yield CommonEvent(type=ERROR, data={"message": "exited", "ending": exited.as_dict()})


def _delegated(task_id: str) -> shared.TaskDefinition:
    db_tasks.create_dynamic_task(task_id, "agent-x", "count", "count to four", "cli", "one_time",
                                 None, None, 0, 600, None, scope="agent",
                                 notification_mode="none")
    shared._dynamic_task_ids.add(task_id)
    return shared.TaskDefinition(id=task_id, name="count", agent="agent-x", prompt="count to four",
                                 scope="agent", notification_mode="none", delay_seconds=0)


@pytest.fixture(autouse=True)
def _a_boot_that_catches_up(monkeypatch):
    """A restart of a few minutes, and the reports with no spacing."""
    monkeypatch.setattr(platform_marks, "_boot", platform_marks.Boot(
        stamped=True, down_since=datetime.now(timezone.utc) - timedelta(minutes=5)))
    monkeypatch.setattr(run_recovery, "_REPORT_SPACING_S", 0.0)
    run_recovery._failed_at_boot.clear()
    yield
    run_recovery._failed_at_boot.clear()


@pytest.fixture
def results(monkeypatch):
    """Every result a run hands its delegator."""
    got: list[tuple[str, str, str]] = []

    async def _deliver(task, final_status, output, **kw):
        got.append((kw.get("run_id") or "", final_status, output))
    monkeypatch.setattr(delivery, "_deliver_task_result", _deliver)
    return got


async def _stop_the_platform(run_task: asyncio.Task, layer: _CutLayer, chat_id: str) -> None:
    """The graceful stop, as ``startup`` runs it: the ending noted, the runs
    cancelled, the sessions closed."""
    stream_pump.note_platform_ending()
    shared._shutting_down = True
    run_task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await run_task
    await layer.close_session(SID)
    await _settle(chat_id)


async def _pass() -> int:
    """One pass, and what it let go on beside it (a run's report), to the end."""
    taken = await continue_pass.run_pending()
    for task in background.running("continue_pass"):
        await task
    return taken


async def _reports() -> int:
    """The boot's step for the runs it did not take up, to their reports."""
    taken = await run_recovery.report_failed_at_boot()
    for task in background.running("run_recovery"):
        await task
    return taken


def _boot() -> dict:
    """The next process: nothing is stopping, and the boot reads what was
    left on a database a record-keeping server ran on."""
    shared._shutting_down = False
    stream_pump._platform_ending = False
    drain.reset_for_tests()
    left = cut_turns.record_at_boot(clean_stop=True, catches_up=True)
    run_recovery.defer_orphaned_runs(catches_up=True, clean_stop=True)
    return left


def _wakes(chat_id: str) -> list[dict]:
    return [json.loads(r["event_data"]) for r in task_store.get_chat_messages(chat_id)
            if r.get("event_type") == wire.PERSISTED_SCHEDULE_WAKE]


@pytest.mark.asyncio
async def test_a_run_the_stop_cut_is_reopened_and_its_delegator_gets_one_result(
        temp_db, monkeypatch, results):
    run_id, task_id = "run-re1", "t-re1"
    chat_id = f"task-{run_id}"
    layer = _CutLayer()
    task = _delegated(task_id)
    t, _q = _drive(monkeypatch, layer, task, run_id, SID)
    try:
        await asyncio.wait_for(layer.half.wait(), 10)
        started_at = temp_db.get_run(run_id)["started_at"]
        await _stop_the_platform(t, layer, chat_id)

        # The stop left the run as it was: live, its task row kept, its
        # delegator told nothing, the turn's record on its row.
        row = temp_db.get_run(run_id)
        assert (row["status"], row["error_message"]) == (run_status.RUNNING, None)
        assert task_store.get_dynamic_task(task_id) is not None
        assert results == []
        left = open_turns.get_open(chat_id)
        assert (left["kind"], left["run_id"], left["session_id"]) == ("task", run_id, SID)
        assert task_id not in shared._active_task_ids and run_id not in shared._running_tasks

        assert _boot() == {"waiting": 0, "pending": 1, "ended": 0}
        assert temp_db.get_run(run_id)["status"] == run_status.RUNNING

        layer.cut, layer._closed = False, asyncio.Event()
        assert await _pass() == 1
        reopened = shared._running_tasks[run_id]
        assert shared._active_task_ids[task_id] == run_id      # a fire would not run beside it
        await asyncio.wait_for(reopened, 10)
        await _settle(chat_id)
        await chat_writer.drain(chat_id, timeout=5)
    finally:
        shared._running_tasks.pop(run_id, None)
        shared._active_task_ids.pop(task_id, None)
        shared._run_subscribers.pop(run_id, None)
        shared._run_event_buffer.pop(run_id, None)
        shared._dynamic_task_ids.discard(task_id)

    row = temp_db.get_run(run_id)
    assert row["status"] == run_status.COMPLETED
    assert row["started_at"] == started_at                 # the first start's
    # The engine was told once, in the platform's words, and no tool or
    # prompt of the first start was sent again.
    assert layer.prompts == ["count to four", continue_pass.CONTINUE_WORDS]
    assert _wakes(chat_id) == [{"prompt": continue_pass.CONTINUE_WORDS,
                                "source": wire.WAKE_SOURCE_CONTINUE}]
    # One result, and it holds the whole round.
    assert [(r, s) for r, s, _ in results] == [(run_id, run_status.COMPLETED)]
    assert "Half the work." in results[0][2] and "and the rest." in results[0][2]
    assert continue_pass.CONTINUE_WORDS not in results[0][2]
    assert task_store.get_dynamic_task(task_id) is None    # the one-time row went with its run
    [record] = [r for r in cut_records.in_state(cut_records.CONTINUED)]
    assert (record["run_id"], record["cause"], record["reason"]) == (
        run_id, cut_records.CAUSE_STOP, "its run was reopened")


@pytest.mark.asyncio
async def test_a_reopened_run_with_no_conversation_left_runs_its_own_prompt(
        temp_db, monkeypatch, results):
    run_id, task_id = "run-re2", "t-re2"
    chat_id = f"task-{run_id}"
    layer = _CutLayer()
    t, _q = _drive(monkeypatch, layer, _delegated(task_id), run_id, SID)
    try:
        await asyncio.wait_for(layer.half.wait(), 10)
        await _stop_the_platform(t, layer, chat_id)
        _boot()
        layer.cut, layer._closed, layer.resume = False, asyncio.Event(), ResumeVerdict.FRESH
        await _pass()
        await asyncio.wait_for(shared._running_tasks[run_id], 10)
        await _settle(chat_id)
    finally:
        shared._running_tasks.pop(run_id, None)
        shared._active_task_ids.pop(task_id, None)
        shared._run_subscribers.pop(run_id, None)
        shared._run_event_buffer.pop(run_id, None)
        shared._dynamic_task_ids.discard(task_id)
    # Nothing of the cut round is there to continue: the task's own words.
    assert layer.prompts == ["count to four", "count to four"]
    assert temp_db.get_run(run_id)["session_id"] != SID
    assert [(r, s) for r, s, _ in results] == [(run_id, run_status.COMPLETED)]


@pytest.mark.asyncio
async def test_a_run_that_cannot_be_reopened_fails_and_its_chat_gets_the_card(
        temp_db, monkeypatch, results):
    run_id, task_id = "run-re3", "t-re3"
    chat_id = f"task-{run_id}"
    layer = _CutLayer()
    t, _q = _drive(monkeypatch, layer, _delegated(task_id), run_id, SID)
    try:
        await asyncio.wait_for(layer.half.wait(), 10)
        await _stop_the_platform(t, layer, chat_id)
        _boot()
        db_tasks.delete_dynamic_task(task_id)              # the task was removed meanwhile
        await _pass()
        await chat_writer.drain(chat_id, timeout=5)
    finally:
        shared._run_subscribers.pop(run_id, None)
        shared._run_event_buffer.pop(run_id, None)
        shared._dynamic_task_ids.discard(task_id)
    row = temp_db.get_run(run_id)
    assert (row["status"], row["error_message"]) == (
        run_status.FAILED, "Cut short when the platform restarted")
    assert run_id not in shared._running_tasks
    [record] = cut_records.in_state(cut_records.DROPPED)
    assert record["reason"] == "its run could not be reopened"
    cards = [json.loads(r["event_data"]) for r in task_store.get_chat_messages(chat_id)
             if r.get("event_type") == wire.SYSTEM]
    assert [c["reason"] for c in cards if c.get("subtype") == wire.SUBTYPE_TURN_ENDED] == [
        turn_ending.RESTARTED]


@pytest.mark.asyncio
async def test_a_person_who_stopped_the_run_under_the_stop_is_not_left_for_a_reopen(
        temp_db, monkeypatch, results):
    run_id, task_id = "run-re4", "t-re4"
    chat_id = f"task-{run_id}"
    layer = _CutLayer()
    t, _q = _drive(monkeypatch, layer, _delegated(task_id), run_id, SID)
    try:
        await asyncio.wait_for(layer.half.wait(), 10)
        stream_pump.note_platform_ending()
        shared._shutting_down = True
        assert await scheduler.cancel_run(run_id)          # the person's Stop
        await asyncio.wait_for(t, 10)
        await _settle(chat_id)
    finally:
        shared._shutting_down = False
        shared._run_subscribers.pop(run_id, None)
        shared._run_event_buffer.pop(run_id, None)
        shared._dynamic_task_ids.discard(task_id)
    assert temp_db.get_run(run_id)["status"] == run_status.CANCELLED


# --- which runs the boot keeps ----------------------------------------------------


def _run(run_id: str, *, status: str = "running", task_type: str | None = None,
         chat: bool = True, completed: bool = False) -> None:
    task_store.create_run(run_id, "task-x", "pa", "manual", None, "do it", task_type)
    fields: dict = {"status": status}
    if chat:
        chat_id = f"task-{run_id}"
        task_store.create_chat(chat_id, "user-1", "pa", "default")
        fields |= {"chat_id": chat_id, "session_id": f"sid-{run_id}"}
    if completed:
        fields["completed_at"] = "2026-01-01T00:00:00+00:00"
    task_store.update_run(run_id, **fields)


@pytest.mark.asyncio
async def test_the_words_read_what_the_pump_saved_of_each_call(temp_db, monkeypatch, results):
    """The rows the pump writes, not rows made for the test: the rider is the
    only record of its calls an engine that rebuilds from rows has."""
    run_id, task_id = "run-calls", "t-calls"
    chat_id = f"task-{run_id}"
    layer = _ToolsLayer()
    t, _q = _drive(monkeypatch, layer, _delegated(task_id), run_id, SID)
    try:
        await asyncio.wait_for(layer.half.wait(), 10)
        await _stop_the_platform(t, layer, chat_id)
        await chat_writer.drain(chat_id, timeout=5)
    finally:
        shared._running_tasks.pop(run_id, None)
        shared._active_task_ids.pop(task_id, None)
        shared._run_subscribers.pop(run_id, None)
        shared._run_event_buffer.pop(run_id, None)
        shared._dynamic_task_ids.discard(task_id)
    assert continue_pass._tool_calls_since(chat_id, "") == [
        "Write (answered)", "mcp__mail__send (no answer, it may have finished)"]


def test_on_a_stamped_database_the_boot_keeps_what_reopens(temp_db):
    _run("run-local")
    _run("run-pending", status="pending", chat=False)
    _run("run-judge", task_type=task_kinds.RUN_CHECK)
    _run("run-had-ended", completed=True)
    parked, failed = run_recovery.defer_orphaned_runs(catches_up=True)
    assert (parked, failed) == (0, 2)
    assert temp_db.get_run("run-local")["status"] == "running"
    assert temp_db.get_run("run-pending")["status"] == "pending"
    assert temp_db.get_run("run-judge")["status"] == "failed"
    assert temp_db.get_run("run-had-ended")["status"] == "failed"
    # The kept run that had started has a record the pass takes. One that
    # never started needs none.
    [record] = cut_records.in_state(cut_records.PENDING)
    assert (record["chat_id"], record["run_id"], record["kind"], record["session_id"]) == (
        "task-run-local", "run-local", "task", "sid-run-local")
    # A second boot makes no second record.
    run_recovery.defer_orphaned_runs(catches_up=True)
    assert len(cut_records.in_state(cut_records.PENDING)) == 1


def _told(monkeypatch) -> list[str]:
    """The run ids the recovery reports, with a task row for each."""
    from services.scheduler import scheduler
    said: list[str] = []

    async def _deliver(task, final, output, **kw):
        said.append(kw.get("run_id"))
    monkeypatch.setattr(scheduler, "_deliver_task_result", _deliver)
    monkeypatch.setattr(task_store, "get_dynamic_task", lambda task_id: {"id": task_id})
    monkeypatch.setattr(scheduler, "_row_to_task", lambda row: row)
    return said


def test_a_boot_that_does_not_catch_up_fails_a_run_with_its_one_report(temp_db, monkeypatch):
    """A first start, a restore, a long downtime: no run is reopened. One that
    started has a record, whose card ends it. One that never started, or
    has nothing a record could hang on, fails at once and is told after."""
    told = _told(monkeypatch)
    _run("run-old")
    _run("run-old-pending", status="pending", chat=False)
    _run("run-old-no-chat", chat=False)
    parked, failed = run_recovery.defer_orphaned_runs(catches_up=False)
    assert (parked, failed) == (0, 2)
    # Live until its report, so a process that ends here reports it next time.
    assert temp_db.get_run("run-old-pending")["status"] == run_status.PENDING
    assert temp_db.get_run("run-old-no-chat")["status"] == run_status.RUNNING
    assert temp_db.get_run("run-old-no-chat")["error_message"] == run_recovery.RUN_CUT
    [record] = cut_records.in_state(cut_records.PENDING)
    assert (record["run_id"], record["cause"]) == ("run-old", cut_records.CAUSE_NOT_TAKEN_UP)
    assert temp_db.get_run("run-old")["status"] == run_status.RUNNING
    assert told == []
    assert asyncio.run(_pass()) == 1
    assert temp_db.get_run("run-old")["error_message"] == run_recovery.RUN_CUT
    assert cut_records.in_state(*cut_records.UNSETTLED) == [] and told == ["run-old"]
    assert asyncio.run(_reports()) == 2
    assert sorted(told) == ["run-old", "run-old-no-chat", "run-old-pending"]
    assert temp_db.get_run("run-old-pending")["status"] == run_status.FAILED
    assert asyncio.run(_reports()) == 0 and len(told) == 3


def test_a_pending_run_the_boot_fails_is_never_started(temp_db, monkeypatch):
    """The boot's own steps in their order, with the reports still spaced when
    ``start_pending_runs`` reads the runs: a held fire past the age rule does
    not run under its report."""
    told = _told(monkeypatch)
    monkeypatch.setattr(run_recovery, "_REPORT_SPACING_S", 0.1)
    started: list[str] = []
    monkeypatch.setattr(runner, "reopen", lambda run, task, prompt, fresh=False: started.append(run["id"]) or True)
    _run("run-p1", status="pending", chat=False)
    _run("run-p2", status="pending", chat=False)
    run_recovery.defer_orphaned_runs(catches_up=False)

    async def scene():
        await run_recovery.report_failed_at_boot()
        await continue_pass.settle_waiters()
        assert await run_recovery.start_pending_runs() == 0
        for task in background.running("run_recovery"):
            await task
    asyncio.run(scene())
    assert started == [] and sorted(told) == ["run-p1", "run-p2"]
    for run_id in ("run-p1", "run-p2"):
        assert temp_db.get_run(run_id)["status"] == run_status.FAILED


def test_a_pending_run_at_a_boot_that_catches_up_still_starts(temp_db, monkeypatch):
    _told(monkeypatch)
    started: list[str] = []
    monkeypatch.setattr(runner, "reopen", lambda run, task, prompt, fresh=False: started.append(run["id"]) or True)
    _run("run-held", status="pending", chat=False)
    assert run_recovery.defer_orphaned_runs(catches_up=True) == (0, 0)
    assert asyncio.run(run_recovery.start_pending_runs()) == 1
    assert started == ["run-held"]


def test_what_a_boot_refused_stays_refused_on_the_next_boot(temp_db, monkeypatch):
    """The process ends in the boot's settle, before the pass: its answer is in
    the store, and the stamp it wrote does not read the dump as caught up. A
    held fire never starts, and each run still has its one report."""
    told = _told(monkeypatch)
    started: list[str] = []
    monkeypatch.setattr(runner, "reopen", lambda run, task, prompt, fresh=False: started.append(run["id"]) or True)
    _run("run-dump")
    _run("run-dump-pending", status="pending", chat=False)
    _run("run-dump-no-chat", chat=False)
    first = platform_marks.read_at_boot()
    assert not first.catches_up
    cut_turns.record_at_boot(clean_stop=first.clean_stop, catches_up=first.catches_up)
    run_recovery.defer_orphaned_runs(catches_up=first.catches_up, clean_stop=first.clean_stop)
    platform_marks.stamp_at_boot()
    run_recovery._failed_at_boot.clear()

    second = platform_marks.read_at_boot()
    assert second.catches_up
    cut_turns.record_at_boot(clean_stop=second.clean_stop, catches_up=second.catches_up)
    run_recovery.defer_orphaned_runs(catches_up=second.catches_up, clean_stop=second.clean_stop)
    [record] = cut_records.in_state(cut_records.PENDING)
    assert (record["run_id"], record["cause"]) == ("run-dump", cut_records.CAUSE_NOT_TAKEN_UP)
    assert continue_pass.judge(record).action == continue_pass.CARD
    assert asyncio.run(run_recovery.start_pending_runs()) == 0 and started == []
    asyncio.run(_pass())
    assert asyncio.run(_reports()) == 2
    assert sorted(told) == ["run-dump", "run-dump-no-chat", "run-dump-pending"]
    for run_id in told:
        assert temp_db.get_run(run_id)["status"] == run_status.FAILED


def test_a_run_ended_by_the_pass_and_a_lapsed_deadline_at_once_is_told_once(temp_db, monkeypatch):
    """Two that end one run, side by side: its record's card, and the sweep of
    a parked run whose machine never came back."""
    told = _told(monkeypatch)
    _run("run-both")
    cut_records.add("task-run-both", cause=cut_records.CAUSE_NOT_TAKEN_UP, state=cut_records.PENDING,
                    kind="task", run_id="run-both", session_id="sid-run-both")

    async def scene():
        await asyncio.gather(_pass(), run_recovery._fail_and_report(
            "run-both", "task-run-both", run_recovery.RUN_CUT))
    asyncio.run(scene())
    assert told == ["run-both"]
    assert temp_db.get_run("run-both")["status"] == run_status.FAILED
    assert cut_records.in_state(*cut_records.UNSETTLED) == []


def test_a_process_ended_between_the_fail_and_the_report_reports_nothing(temp_db, monkeypatch):
    """The known limit (the roadmap's line): the next pass finds the run over.
    It is never told twice, and the run's row says how it ended."""
    told = _told(monkeypatch)
    _run("run-gap")
    assert task_store.fail_live_run("run-gap", run_recovery.RUN_CUT)
    asyncio.run(run_recovery._fail_and_report("run-gap", "task-run-gap", run_recovery.RUN_CUT))
    assert told == []
    assert temp_db.get_run("run-gap")["error_message"] == run_recovery.RUN_CUT


def test_of_many_that_race_to_fail_a_run_one_is_told_it_did(temp_db, monkeypatch):
    """And a run's listeners (the apps' ``tasks`` feed) hear of its end once,
    as from any other terminal write."""
    _run("run-race")
    gate = threading.Barrier(8)
    heard: list[tuple[str, str]] = []
    monkeypatch.setattr(db_tasks, "_run_finished_hooks", [lambda run_id, status: heard.append((run_id, status))])

    def _try(_):
        gate.wait()
        return task_store.fail_live_run("run-race", run_recovery.RUN_CUT)
    with ThreadPoolExecutor(8) as pool:
        assert sorted(pool.map(_try, range(8))) == [False] * 7 + [True]
    assert heard == [("run-race", run_status.FAILED)]
    assert not task_store.fail_live_run("run-race", "again")
    assert len(heard) == 1
    assert temp_db.get_run("run-race")["error_message"] == run_recovery.RUN_CUT


def test_the_marks_of_a_restored_dump_keep_no_run_for_a_reopen(temp_db, monkeypatch):
    """The stamp of a dump 90 days old is read as it is on the real boot."""
    told = _told(monkeypatch)
    _run("run-dump")
    task_store.set_platform_setting(platform_marks.ALIVE_AT, (
        datetime.now(timezone.utc) - timedelta(days=90)).isoformat())
    boot = platform_marks.read_at_boot()
    assert boot.stamped and not boot.catches_up
    run_recovery.defer_orphaned_runs(catches_up=boot.catches_up, clean_stop=boot.clean_stop)
    assert asyncio.run(_pass()) == 1
    assert temp_db.get_run("run-dump")["status"] == run_status.FAILED and told == ["run-dump"]
    assert cut_records.in_state(*cut_records.UNSETTLED) == []
    assert asyncio.run(_reports()) == 0


def test_a_kept_run_that_cannot_have_a_record_fails_with_its_report(temp_db, monkeypatch):
    """A stop between the run's admission and its first row leaves it with no
    chat and no session, and a chat can be deleted under a run: no record
    would ever end it."""
    told = _told(monkeypatch)
    for run_id, facts in (("run-nosid", {}),
                          ("run-nochat", {"chat_id": "task-gone", "session_id": SID})):
        task_store.create_run(run_id, "task-x", "agent-x", "manual", None, "do it", "delegate", "agent", None)
        task_store.update_run(run_id, status=run_status.RUNNING, **facts)
    run_recovery.defer_orphaned_runs(catches_up=True, clean_stop=True)
    assert cut_records.in_state(*cut_records.UNSETTLED) == []
    assert asyncio.run(_reports()) == 2
    assert sorted(told) == ["run-nochat", "run-nosid"]
    for run_id in ("run-nosid", "run-nochat"):
        assert temp_db.get_run(run_id)["status"] == run_status.FAILED


def test_a_runs_own_record_is_found_by_its_session_too(temp_db):
    """A pump that did not name the run (a wake's, an older release's)
    recorded the run's turn: the record is the run's, not a second one."""
    _run("run-sess")
    open_turns.open_turn("task-run-sess", "tok", "sid-run-sess", kind="task")
    cut_turns.record_at_boot(clean_stop=False, catches_up=True)
    run_recovery.defer_orphaned_runs(catches_up=True)
    [record] = cut_records.in_state(cut_records.PENDING)
    assert record["run_id"] == "run-sess"


def test_the_stops_net_keeps_what_the_next_boot_takes_up(temp_db):
    _run("run-keeps")
    _run("run-app", task_type=task_kinds.RUN_APP)
    _run("run-check", task_type=task_kinds.RUN_CHECK)
    assert run_recovery.give_up_at_shutdown() == ["run-keeps"]


# --- a fire the platform held, a run that never started ------------------------------


async def _no_cap(task) -> str:
    return ""


@pytest.mark.asyncio
async def test_a_fire_under_a_hold_is_a_pending_run_that_starts_at_the_resume(
        temp_db, monkeypatch):
    launched: list[tuple] = []
    monkeypatch.setattr(runner, "_launch_run", lambda run_id, sid, task, prompt, *a, **kw:
                        launched.append((run_id, prompt, kw.get("reopened"))))
    monkeypatch.setattr(runner, "_pool_cap_block", _no_cap)
    monkeypatch.setattr("configuration.models.get_cli_model", lambda agent, layer=None: "test-model")
    task = _delegated("t-held")
    try:
        await drain.drain(60)
        run_id = await runner._execute_task(task, trigger_type="manual")
        # The caller has its run id, the run row and its chat are there, and
        # nothing started. The one-time row is marked fired, as at any launch.
        row = temp_db.get_run(run_id)
        assert row["status"] == run_status.PENDING and launched == []
        assert task_store.get_chat(f"task-{run_id}") is not None
        assert task_store.get_dynamic_task("t-held")["fired"]
        # A second fire during the hold gets this run back and mints no other.
        assert shared._active_task_ids["t-held"] == run_id
        assert await runner._execute_task(task, trigger_type="manual") == run_id
        assert len(task_store.list_runs(task_id="t-held")) == 1
        assert await run_recovery.start_pending_runs() == 0     # still held
        await drain.resume()
        assert await run_recovery.start_pending_runs() == 1
        assert launched == [(run_id, "count to four", None)]
        assert shared._active_task_ids["t-held"] == run_id
    finally:
        shared._active_task_ids.pop("t-held", None)
        shared._dynamic_task_ids.discard("t-held")


@pytest.mark.asyncio
async def test_a_follow_up_a_drain_held_keeps_its_lanes_conversation(temp_db, monkeypatch):
    """A delegator's continue during a hold: the worker's session stays in the
    index, and the run starts on it at the resume. On a new id the round would
    find nothing to resume and the worker would lose all it knew."""
    from core.session import session_state as st
    from services.scheduler import scheduler
    launched: list[tuple] = []
    monkeypatch.setattr(runner, "_launch_run", lambda run_id, sid, task, prompt, *a, **kw:
                        launched.append((run_id, sid, task.continue_session)))
    monkeypatch.setattr(runner, "_pool_cap_block", _no_cap)
    monkeypatch.setattr("configuration.models.get_cli_model", lambda agent, layer=None: "test-model")
    task_store.create_chat("worker-chat", "task::agent-x", "agent-x", "default")
    task_store.update_chat("worker-chat", session_id=SID)
    db_tasks.create_dynamic_task("t-follow", "agent-x", "count", "go on", "cli", "one_time", None, None, 0, 600,
                                 None, scope="agent", notification_mode="none", continue_session=SID,
                                 target_chat_id="worker-chat")
    task = scheduler._row_to_task(task_store.get_dynamic_task("t-follow"))
    st._sessions[SID] = {"created": True, "message_count": 7, "is_task": True}
    try:
        await drain.drain(60)
        run_id = await runner._execute_task(task, trigger_type="manual")
        assert launched == [] and st._sessions[SID]["message_count"] == 7
        await drain.resume()
        await run_recovery.start_pending_runs()
        assert launched == [(run_id, SID, SID)]
    finally:
        st._sessions.pop(SID, None)
        shared._active_task_ids.pop("t-follow", None)
        shared._dynamic_task_ids.discard("t-follow")


@pytest.mark.asyncio
async def test_a_fire_that_waited_for_its_slot_reads_the_hold_after_the_wait(temp_db, monkeypatch):
    """The spacing wait can outlast the start of a drain."""
    from services.scheduler import firing
    launched: list[str] = []
    monkeypatch.setattr(runner, "_launch_run", lambda run_id, *a, **kw: launched.append(run_id))
    monkeypatch.setattr(runner, "_pool_cap_block", _no_cap)
    monkeypatch.setattr("configuration.models.get_cli_model", lambda agent, layer=None: "test-model")

    async def _slot(task_id):
        await drain.drain(60)
    monkeypatch.setattr(firing, "_spawn_spacing_gate", _slot)
    db_tasks.create_dynamic_task("t-space", "agent-x", "count", "go on", "cli", "cron", "0 3 1 1 *", None, 0, 600,
                                 None, scope="agent", notification_mode="none")
    from services.scheduler import scheduler
    task = scheduler._row_to_task(task_store.get_dynamic_task("t-space"))
    try:
        run_id = await runner._execute_task(task, trigger_type="scheduled")
        assert launched == [] and temp_db.get_run(run_id)["status"] == run_status.PENDING
    finally:
        shared._active_task_ids.pop("t-space", None)
        shared._dynamic_task_ids.discard("t-space")


@pytest.mark.asyncio
async def test_a_judge_run_passes_a_hold(temp_db, monkeypatch):
    launched: list[str] = []
    monkeypatch.setattr(runner, "_launch_run", lambda run_id, *a, **kw: launched.append(run_id))
    monkeypatch.setattr(runner, "_pool_cap_block", _no_cap)
    monkeypatch.setattr("configuration.models.get_cli_model", lambda agent, layer=None: "test-model")
    judge = shared.TaskDefinition(id="t-judge", name="judge", agent="agent-x", prompt="judge it",
                                  scope="agent", notification_mode="none",
                                  task_type=task_kinds.CHECK)
    try:
        await drain.drain(60)
        run_id = await runner._execute_task(judge, trigger_type="manual")
        assert launched == [run_id]
    finally:
        shared._active_task_ids.pop("t-judge", None)


@pytest.mark.asyncio
async def test_a_pending_run_whose_task_is_gone_fails_and_a_paused_one_waits(temp_db, monkeypatch):
    launched: list[str] = []
    monkeypatch.setattr(runner, "reopen", lambda run, task, prompt, fresh=False:
                        launched.append(run["id"]) or True)
    _run("run-gone", status="pending", chat=False)
    task_store.create_run("run-paused", "t-paused", "pa", "manual", None, "do it")
    db_tasks.create_dynamic_task("t-paused", "pa", "p", "p", "cli", "one_time", None, None, 0,
                                 600, None, scope="agent")
    with task_store.get_conn() as conn:
        conn.execute("UPDATE dynamic_tasks SET enabled=FALSE WHERE id='t-paused'")
        conn.commit()
    assert await run_recovery.start_pending_runs() == 0
    assert temp_db.get_run("run-gone")["error_message"] == (
        "The task was removed before its run started")
    assert temp_db.get_run("run-paused")["status"] == "pending" and launched == []


@pytest.mark.asyncio
async def test_a_run_cancelled_in_the_queue_by_the_stop_stays_pending(temp_db):
    task_store.create_run("run-q", "task-x", "pa", "manual", None, "do it")
    task_store.create_run("run-q-user", "task-x", "pa", "manual", None, "do it")
    shared._shutting_down = True
    shared._user_cancelled_runs.add("run-q-user")
    try:
        await runner._end_unstamped("run-q")
        await runner._end_unstamped("run-q-user")
    finally:
        shared._shutting_down = False
        shared._user_cancelled_runs.discard("run-q-user")
    assert temp_db.get_run("run-q")["status"] == run_status.PENDING
    assert temp_db.get_run("run-q-user")["status"] == run_status.CANCELLED


@pytest.mark.asyncio
async def test_reopen_refuses_a_task_that_runs_already(temp_db, monkeypatch):
    monkeypatch.setattr(runner, "_launch_run", lambda *a, **kw: None)
    task = shared.TaskDefinition(id="t-busy", name="n", agent="pa", prompt="p")
    shared._active_task_ids["t-busy"] = "run-other"
    try:
        assert runner.reopen({"id": "run-x", "session_id": SID}, task, "words") is False
    finally:
        shared._active_task_ids.pop("t-busy", None)


def test_the_cursor_of_a_reopened_run_is_its_first_starts(temp_db):
    from datetime import datetime, timezone
    task_store.create_chat("c-cursor", "user-1", "pa", "default")
    before = task_store.add_chat_messages_batch("c-cursor", [("assistant", "an earlier round", "", "")])
    started = datetime.now(timezone.utc).isoformat()
    task_store.add_chat_messages_batch("c-cursor", [("assistant", "this round", "", "")])
    assert run_recovery.start_cursor({"started_at": started}, "c-cursor") == before
