"""A live run no task of this process drives: a person's Stop of one, and
the sweep that ends one nothing carries on.

Such a run waits for its replay (parked at boot, its machine not back yet) or
for its reopen (the continue pass has not started it again), and its chat's
unsettled record names it: the record's owner carries it on. Any other run
there is a fault, and these two end it: a person's Stop at once (``stop``,
from ``scheduler.cancel_run``), the recovery's sweep in bounded time
(``sweep``, from ``run_recovery.sweep_expired`` on the reaper's tick), so
its delegator is always told.

One server process per database: a second process would read the first one's
runs as driven by nobody, and each would end the other's within four minutes.
The boot's recovery assumes the same.
"""

from __future__ import annotations

import asyncio
import logging
import time

from core.session import session_kind
from services.scheduler import cut_turns, run_recovery as rr
from storage import database as task_store
from storage.automation import run_status
from storage.chat import cut_records

logger = logging.getLogger("otodock.recovery")

#: How long a Stop that lost the record to a reopen looks for the reopened
#: run's task, which it then cancels the ordinary way.
_STOP_FIND_TASK_S = 1.0

#: A live run with nothing to carry it on is ended by the sweep only when two
#: sweeps at least this far apart found it so (the reaper ticks every 60 s).
STRANDED_GRACE_S = 120.0
# run id -> when a sweep first found it with nothing to carry it on.
_stranded_since: dict[str, float] = {}


async def stop(run_id: str) -> bool:
    """A person's Stop of a run no task of this process drives. Such a run
    waits for its replay (parked at boot, its machine not back yet) or for
    its reopen (the continue pass has not started it again), and its chat's
    unsettled record names it. Or nothing carries it on any more (a hole the
    stranded sweep would end): the page shows it running, so the Stop stops
    it too. It answers False only for a run there is nothing to stop, and
    says why in the log.

    With the run's record (one that names it, or a ``task`` record of its
    session that names no run): the record is settled as dropped, so nothing
    reopens or replays the run. The settle is the one statement the Stop and
    the reopen both need (``cut_turns.stopped_by_person``,
    ``taken_for_reopen``, which ``reopen_run`` asks before it launches): one
    of them wins. A Stop that lost it cancels the reopened run's task, the
    ordinary way. The run's own session is closed where a layer holds it.
    One nothing holds yet (its machine has not reported since the restart)
    loses its context, which is the platform's record that a session was
    closed on purpose: its machine ends the process when it reports. A
    session that is a person's is never touched (``_is_the_runs_own_session``).

    With no record of its own, for a run that reopens: the run ends
    cancelled and its delegator is told, and no session is touched, since
    nothing says whose the session is now (a later round of the lane may run
    on it). Another record of the chat stays: it is not this run's. Its
    reads await, so it looks at the task registry again before it acts: a
    run a reopen launched meanwhile is cancelled as a live task, and a run
    whose replay is writing its end is left to that end. True when the run
    was stopped."""
    run = await asyncio.to_thread(task_store.get_run, run_id)
    if not run or not run_status.is_live(run.get("status")):
        logger.info("Recovery: a Stop of run %s found nothing to stop: it is not live", run_id)
        return False
    chat_id = run.get("chat_id") or ""
    record = await asyncio.to_thread(cut_records.unsettled_of, chat_id) if chat_id else None
    if record is not None and _record_of_the_run(record, run):
        return await _stop_at_the_record(run, record)
    if run.get("status") != run_status.RUNNING or not rr.reopens(run) or rr._refused_by_a_boot(run):
        logger.info("Recovery: a Stop of run %s found nothing to stop here: no task drives it "
                    "and it is %s", run_id,
                    "waiting to start" if run.get("status") != run_status.RUNNING
                    else "not one that goes on after a cut")
        return False
    # The reads above awaited: a reopen may have launched the run since the
    # caller looked (its record settled a moment ago), and an adoption may be
    # writing its end.
    from services.scheduler import shared
    task = shared._running_tasks.get(run_id)
    if task is not None and not task.done():
        shared._user_cancelled_runs.add(run_id)
        task.cancel()
        return True
    if run_id in rr._driving:
        logger.info("Recovery: a Stop of run %s found nothing to stop here: its replay is "
                    "ending it", run_id)
        return False
    async with rr._lock:
        for sid, rec in list(rr._parked.items()):
            if rec.get("run_id") == run_id:
                rr._parked.pop(sid, None)
    if not await asyncio.to_thread(task_store.cancel_live_run, run_id, "Interrupted by user"):
        return False
    logger.info("Recovery: run %s stopped by a person while nothing carried it on (no record "
                "of its own)", run_id)
    await rr.report_stopped(run)
    return True


def _record_of_the_run(record: dict, run: dict) -> bool:
    """The unsettled record is this run's cut turn: it names the run, or it
    is a task's record of the run's session that names no run (a pump that
    did not name it). A person's own cut turn on the session is not."""
    if (record.get("run_id") or "") == run["id"]:
        return True
    return (not record.get("run_id") and record.get("kind") == session_kind.TASK.name
            and bool(run.get("session_id")) and record.get("session_id") == run.get("session_id"))


async def _stop_at_the_record(run: dict, record: dict) -> bool:
    """The Stop of a run its record names: see ``stop``."""
    from core.session import session_state
    from core.session.session_manager import find_layer_for_session
    from services.scheduler import shared
    run_id = run["id"]
    if not await cut_turns.stopped_by_person(record):
        # The pass took the record for its reopen a moment ago.
        waited = 0.0
        while waited < _STOP_FIND_TASK_S:
            task = shared._running_tasks.get(run_id)
            if task is not None and not task.done():
                shared._user_cancelled_runs.add(run_id)
                task.cancel()
                return True
            await asyncio.sleep(0.05)
            waited += 0.05
        logger.info("Recovery: a Stop of run %s lost its record to the reopen and found no task",
                    run_id)
        return False
    sid = record.get("session_id") or run.get("session_id") or ""
    async with rr._lock:
        rr._parked.pop(sid, None)
    if not await asyncio.to_thread(task_store.cancel_live_run, run_id, "Interrupted by user"):
        return False
    own = bool(sid) and _is_the_runs_own_session(record, sid)
    layer = find_layer_for_session(sid) if own else None
    try:
        if layer is not None:
            await layer.close_session(sid)
        elif own and not rr._taken_meanwhile(sid):
            # (a start of the id in flight owns what is kept under it)
            await session_state.forget_session(sid)
    except Exception:  # noqa: BLE001  # the run is stopped: its session's close must not undo that
        logger.warning("Recovery: the session of the stopped run %s was not closed", run_id,
                       exc_info=True)
    logger.info("Recovery: run %s stopped by a person while it waited (%s record)", run_id,
                record["state"])
    await rr.report_stopped(run)
    return True


def _is_the_runs_own_session(record: dict, session_id: str) -> bool:
    """Whether a Stop of the run this record names may end ``session_id``:
    only the run's own headless session. A round that drove a person's live
    terminal has the terminal's session on its row (``TaskRun._open_round``),
    and the boot names the run on the terminal's record, or writes the run a
    record with that session (``_ensure_record``). An ordinary Stop of such
    a run closes nothing of the terminal, so this one does not either: a
    terminal's record and a session the session index knows as no task's
    are a person's. The run's own session has a task's entry there, or none
    (its task dropped it at its end). A terminal the platform holds now is
    in no layer's registry, and ``session_is_held`` keeps its context."""
    from core.session import session_state
    if record.get("kind") == cut_turns.KIND_TERMINAL:
        return False
    entry = session_state._sessions.get(session_id)
    return entry is None or bool(entry.get("is_task"))


async def sweep() -> int:
    """A live run nothing carries on is ended with its typed ending and one
    report. Such a run is ``running``, reopens, no boot refused it, no task
    of this process drives it, it is not parked, no adoption of this process
    drives it (``run_recovery._driving``), it does not drive a terminal the platform
    holds, and its chat has no unsettled record of it. Every way a run gets
    there is a fault: this ends it, in bounded time, with its delegator
    told, when two sweeps ``STRANDED_GRACE_S`` apart found it so. The words
    follow what cut the chat's last turn (a machine, else the platform).
    Returns how many it ended."""
    from core.session import interactive_session
    from services.scheduler import shared
    now = time.monotonic()
    parked = {rec.get("run_id") for rec in rr._parked.values()}
    candidates = [
        run for run in await asyncio.to_thread(task_store.list_orphaned_runs)
        if run.get("status") == run_status.RUNNING and rr.reopens(run)
        and not rr._refused_by_a_boot(run) and run["id"] not in parked
        and run["id"] not in rr._driving
        and (shared._running_tasks.get(run["id"]) is None
             or shared._running_tasks[run["id"]].done())
        and not _drives_a_live_terminal(interactive_session, run.get("session_id") or "")]
    seen: dict[str, float] = {}
    ended = 0
    for run in candidates:
        cause = await asyncio.to_thread(_stranded_cause, run)
        if cause is None:
            continue
        since = _stranded_since.get(run["id"], now)
        seen[run["id"]] = since
        if now - since < STRANDED_GRACE_S:
            continue
        seen.pop(run["id"], None)
        logger.warning("Recovery: run %s was live with nothing to carry it on: ending it with "
                       "its report", run["id"])
        reason = rr.RUN_CUT_MACHINE if cause == cut_records.CAUSE_MACHINE else rr.RUN_CUT
        await rr._fail_and_report(run["id"], run.get("chat_id") or "", reason, cause=cause)
        ended += 1
    _stranded_since.clear()
    _stranded_since.update(seen)
    return ended


def _drives_a_live_terminal(interactive_session, session_id: str) -> bool:
    """The run's round drove a person's terminal the platform holds again:
    the terminal carries it on while it lives."""
    sess = interactive_session.get(session_id) if session_id else None
    return sess is not None and bool(getattr(sess, "alive", False))


def _stranded_cause(run: dict) -> str | None:
    """None when an unsettled record of the run's chat is the run's (its
    owner carries it on); else what cut the chat's last turn, the cause of
    its newest record ("" for none). Synchronous."""
    chat_id = run.get("chat_id") or ""
    if not chat_id:
        return None
    record = cut_records.unsettled_of(chat_id)
    if record is not None and _record_of_the_run(record, run):
        return None
    newest = cut_records.newest_of(chat_id) or {}
    return newest.get("cause") or ""
