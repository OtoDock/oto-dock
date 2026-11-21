"""Mode C — re-attach in-flight remote turns across a proxy restart.

A proxy restart used to blind-fail every in-flight task run at startup
(``mark_orphaned_runs_failed``) and drop the streamed tail; the run showed
as failed even though the satellite kept the CLI alive and the turn either
finished or is still running. This module replaces that for **remote
claude-code-cli** sessions:

1. At startup, ``defer_orphaned_runs`` parks recovery-eligible runs (remote
   CLI, pinned target, chat row present) with a deadline instead of failing
   them; the runs that reopen (``reopens``) are kept for the continue pass,
   and the rest fail.
2. When a satellite reconnects it reports its live headless sessions
   (``sessions_alive``). ``on_sessions_alive`` adopts each parked run whose
   session the satellite still holds — replaying the retained turn buffer
   through a fresh ``ChatStreamPump`` (so dashboard viewers see it stream
   as if never disconnected), then finalizing the run. Plain (non-run)
   chats with a live turn are adopted too, so their tail lands and the turn
   closes. Parked runs whose session a complete report does NOT list go to
   the continue pass, which reopens them.
3. A sweeper fails runs whose recovery deadline lapses with no reconnect.

A run the platform cut that no machine replays is reopened under its own id
(``reopen_run``): its record (``storage/chat/cut_records.py``) is the continue
pass's (``continue_pass.py``), which calls it. Which runs reopen is
``reopens``. A run that never started is started again
(``start_pending_runs``), except at a boot that takes nothing up, whose answer
sits on the run's row and its record, so a later boot reads the same.
"""

from __future__ import annotations

import asyncio
import functools
import logging
import time
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any, cast

import psycopg

from core import placement
from core.background import spawn
from core.permission_modes import ASK_MODE
from core.session import session_kind
from services.scheduler import cut_turns, task_kinds
from storage import database as task_store
from storage.automation import run_status
from storage.chat import cut_records, open_turns

if TYPE_CHECKING:
    from core.events.turn_ending import TurnEnding

logger = logging.getLogger("otodock.recovery")

# How long a parked run waits for its satellite to reconnect before it is
# failed and its delegator told. A report, never a second start: the run's
# session may still be working there, and a machine that comes back later
# replays its turn into the chat.
RECOVERY_DEADLINE_S = 1800.0

# The runs a restart never reopens: an app handler's follows its delivery
# row, and a check's judge run belongs to the turn it judged.
_NEVER_REOPENED = frozenset({task_kinds.RUN_APP, task_kinds.RUN_CHECK, task_kinds.RUN_WRAPUP})

#: Why a run ends when the boot or the pass does not take it up again. A boot
#: compares a live row's reason with this text (``_refused_by_a_boot``).
RUN_CUT = task_store.RUN_CUT_REASON
#: The same for a turn its machine's restart cut, and the sentences of a run
#: the recovery could not take back.
RUN_CUT_MACHINE = "Cut short when its machine restarted"
RUN_NOT_STARTED = "Not started. The platform restarted first."
RUN_MACHINE_NOT_BACK = RUN_CUT + ". Its machine did not come back in time."
RUN_SESSION_CLOSED = RUN_CUT + ". Its session could not be taken back."
#: The output of a cut round whose lane goes on with a round its delegator
#: queued: the caller's card says so, and nobody is woken.
CUT_ROUND_WENT_ON = "A restart cut this round. The next round of this lane carries the work on."
#: How far apart the boot's failed runs report: each may start a turn on its
#: delegator's chat.
_REPORT_SPACING_S = 5.0
# (run id, chat id) of the live runs this boot ends with no record to end them,
# each still live for its one report, its row saying so for the next boot.
_failed_at_boot: list[tuple[str, str]] = []

# session_id -> parked recovery record. A record may or may not carry a run_id
# (a dashboard-driven continuation turn on a task chat has no scheduler task).
_parked: dict[str, dict] = {}
_lock = asyncio.Lock()
# The runs an adoption of this process drives (``_recover_session``), to its
# last line: the stranded sweep leaves them alone.
_driving: set[str] = set()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _session_machine(chat: dict, session_id: str) -> str:
    """The machine a run's session is on: its own record
    (``session_state.placed_on``), else, for a session nothing records, the
    machine its chat's pin names (a pin ``local`` with no record is the
    platform's place: no machine). Synchronous."""
    from core.session.session_state import placed_on
    on = placed_on(session_id, chat.get("id") or "")
    if on is not None:
        return on
    return placement.machine_of(chat.get("execution_target") or "")


def _is_remote_cli_chat(chat: dict) -> bool:
    """Effective-execution-path test for recovery eligibility. The chat column
    may be EMPTY (= agent default — delegate worker chats never stamp it), so
    resolve it the way the spawn path does before comparing; the literal
    comparison silently excluded every delegate lane from Mode C."""
    from core.session.session_manager import (
        get_layer_capabilities,
        resolve_execution_path,
    )
    path = resolve_execution_path(
        chat.get("agent") or "", chat.get("execution_path") or "",
    )
    caps = get_layer_capabilities(path)
    return bool(caps and caps.runtime.supports_reattach_after_restart)


def chat_runs_no_process(chat: dict) -> bool:
    """Whether the chat's engine runs no process of its own (Direct LLM:
    its loop is in the proxy, only its tools run on a machine): nothing of
    its session or turn can be taken back from a machine."""
    from core.session.session_manager import (
        get_layer_capabilities,
        resolve_execution_path,
    )
    caps = get_layer_capabilities(resolve_execution_path(
        chat.get("agent") or "", chat.get("execution_path") or "",
    ))
    return caps is not None and not caps.runtime.has_os_process


def reopens(run: dict) -> bool:
    """Whether a run the platform cut is one that reopens: not an app
    handler's or a judge's, and not a finished run a person's own turn in
    its chat flipped back to running (it has its end time)."""
    return (run.get("task_type") or "") not in _NEVER_REOPENED and not run.get("completed_at")


def start_cursor(run: dict, chat_id: str) -> int:
    """The chat's last row before the run started (the run's output starts
    after it). Synchronous."""
    rows = task_store.get_chat_messages(chat_id) if chat_id else []
    started = run.get("started_at") or ""
    before = [r["id"] for r in rows if (r.get("created_at") or "") < started]
    first = rows[0]["id"] - 1 if rows else 0
    return max(before) if before else first


def defer_orphaned_runs(*, catches_up: bool = False, clean_stop: bool = False) -> tuple[int, int]:
    """Startup: park recovery-eligible orphaned runs, keep the runs that
    reopen, fail the rest.

    A kept run that started has a record of its own (one cut between its
    turns, or before its first row, gets one here), which the pass works
    from. ``catches_up`` (``platform_marks.Boot.catches_up``): false on a
    first start, after a restore or after a long downtime. Such a boot's
    records say it was not taken up, so the pass gives the card and the run's
    one report, on this boot or a later one. A run the pass would not reach
    (one that cannot have a record, or a pending one at such a boot, which
    must not start) says so on its own row, stays live for its one report
    (``report_failed_at_boot``) and is taken the same way by a later boot.
    ``clean_stop``: the last process stopped by its own shutdown.

    Returns ``(parked, failed)``. Must run BEFORE the satellite heartbeat
    monitor so a run is parked before its satellite can report it, and after
    ``cut_turns.record_at_boot``."""
    orphaned = task_store.list_orphaned_runs()
    parked_ids: list[str] = []
    kept: list[dict] = []
    _failed_at_boot.clear()
    for run in orphaned:
        chat_id = run.get("chat_id") or ""
        session_id = run.get("session_id") or ""
        chat = task_store.get_chat(chat_id) if chat_id else None
        target = _session_machine(chat, session_id) if chat and session_id else ""
        eligible = (
            bool(session_id) and bool(chat) and not run.get("completed_at")
            and bool(target)
            and _is_remote_cli_chat(cast("dict", chat))
        )
        if eligible:
            # Its machine already said the session is gone (a record the pass
            # has not taken yet): the pass reopens the run, and no deadline may
            # fail it meanwhile.
            standing = cut_records.unsettled_of(chat_id)
            eligible = standing is None or standing["state"] != cut_records.PENDING
        if eligible:
            _parked[session_id] = {
                "run_id": run["id"],
                "chat_id": chat_id,
                "machine_id": target,
                "agent": run.get("agent") or (chat or {}).get("agent") or "",
                "deadline": time.monotonic() + RECOVERY_DEADLINE_S,
            }
            parked_ids.append(run["id"])
            kept.append({**run, "_machine": target})
        elif reopens(run):
            kept.append(run)
    if not catches_up:
        cause = cut_records.CAUSE_NOT_TAKEN_UP
    else:
        cause = cut_records.CAUSE_STOP if clean_stop else cut_records.CAUSE_CRASH
    for run in kept:
        if not _ensure_record(run, cause, catches_up=catches_up):
            task_store.update_run(run["id"], error_message=RUN_CUT)
            _failed_at_boot.append((run["id"], run.get("chat_id") or ""))
        elif run["id"] not in parked_ids:
            _give_a_deadline(run)
    # Blind-fail every orphaned row EXCEPT the parked and the kept: a run
    # failed this way reports nothing.
    failed = task_store.mark_orphaned_runs_failed(
        exclude_ids=parked_ids + [r["id"] for r in kept])
    if parked_ids or kept:
        logger.info(
            "Startup recovery: parked %d run(s) for satellite re-adopt, kept %d, "
            "failed %d non-recoverable, %d to report", len(parked_ids),
            len(kept) - len(parked_ids) - len(_failed_at_boot), failed, len(_failed_at_boot),
        )
    return len(parked_ids), failed + len(_failed_at_boot)


async def report_failed_at_boot() -> int:
    """Boot, after the pass: each run ``defer_orphaned_runs`` ends fails with
    its one report, spaced, beside the boot. Returns how many."""
    taken = list(_failed_at_boot)
    _failed_at_boot.clear()

    async def _one(delay: float, run_id: str, chat_id: str) -> None:
        if delay > 0:
            await asyncio.sleep(delay)
        await _fail_and_report(run_id, chat_id, RUN_CUT)

    for i, (run_id, chat_id) in enumerate(taken):
        spawn(_one(i * _REPORT_SPACING_S, run_id, chat_id), name="run_failed_report",
              owner="run_recovery")
    return len(taken)


def _give_a_deadline(run: dict) -> None:
    """A kept run on an engine with no replay whose record waits for its
    machine: the deadline of a parked run, or a machine that never reports
    leaves it live with nobody told."""
    record = cut_records.unsettled_of(run.get("chat_id") or "")
    if record and record["state"] == cut_records.WAITING and record.get("run_id") == run["id"]:
        _parked[record["session_id"]] = {
            "run_id": run["id"],
            "chat_id": record["chat_id"],
            "machine_id": record["machine_id"],
            "agent": run.get("agent") or "",
            "deadline": time.monotonic() + RECOVERY_DEADLINE_S,
        }


def _refused_by_a_boot(run: dict) -> bool:
    """A boot decided this live run ends and did not live to report it."""
    return run.get("error_message") == RUN_CUT


def _ensure_record(run: dict, cause: str, *, catches_up: bool) -> bool:
    """A kept run that started (it has a chat and a session) and whose chat
    holds no record of it gets one: on its machine it waits for the report,
    else the pass takes it. A run that never started needs none, and starts
    (``start_pending_runs``) only at a boot that catches up. False for a run
    the boot must end: a running one left with no record (nothing would ever
    end it), a pending one at any other boot, and one an earlier boot said so
    of."""
    if _refused_by_a_boot(run):
        return False
    if run.get("status") != run_status.RUNNING:
        return catches_up
    chat_id, sid = run.get("chat_id") or "", run.get("session_id") or ""
    if not chat_id or not sid:
        return False
    record = cut_records.unsettled_of(chat_id)
    if record is not None and run["id"] == record.get("run_id"):
        return True
    if record is not None and sid == record.get("session_id"):
        # The run's own turn, recorded by a pump that did not name the run
        # (a wake's, an older release's): the record is the run's.
        cut_records.name_run(record["id"], run["id"])
        return True
    machine = run.get("_machine") or ""
    made = cut_records.add(
        chat_id, cause=cause, session_id=sid, kind=session_kind.TASK.name, run_id=run["id"],
        machine_id=machine, started_at=run.get("started_at") or "",
        state=cut_records.WAITING if machine else cut_records.PENDING)
    if made is not None and machine:
        cut_turns._park(made, "")
    return made is not None


def give_up_at_shutdown() -> list[str]:
    """The graceful shutdown's net: the ids of the live runs it leaves for
    the next boot (a turn its machine may replay, a run that reopens).
    Synchronous."""
    return [r["id"] for r in task_store.list_orphaned_runs()
            if reopens(r) or is_recovery_eligible(r.get("chat_id") or "")]


async def on_alive_report(report) -> None:
    """A machine's report of one sign-in (``core.remote.satellite_link``),
    after the adoption took what it takes. The turns the restart cut there
    that nothing adopted get their outcome. A report that is not complete
    decides nothing: the next one does, and a parked run keeps its deadline
    until then."""
    if not report.complete or not report.is_current():
        return
    async with _lock:
        # The adoption took the sessions it replays. What is left gets its
        # outcome below, so no deadline may end it a second time: past the
        # card a person's turn can flip the row back to running.
        left = [sid for sid, rec in _parked.items() if rec["machine_id"] == report.machine_id]
        records = [_parked.pop(sid) for sid in left]
    for rec in records:
        logger.info("Recovery: run %s has its outcome from the report of %s",
                    rec.get("run_id") or "(none)", report.machine_id[:8])
    if await cut_turns.machine_reported(report):
        from services.scheduler import continue_pass
        continue_pass.kick()


def register(connection_manager) -> None:
    """Wire the sessions_alive callback + start the deadline sweeper, and
    read each machine's alive report after it."""
    from core.remote import satellite_link
    connection_manager.set_sessions_alive_callback(on_sessions_alive)
    if all(fn is not on_alive_report for _, fn in satellite_link._alive_hooks):
        satellite_link.add_alive_report_hook(on_alive_report, name="cut_turns")


def is_recovery_eligible(chat_id: str) -> bool:
    """True when a chat's in-flight turn can be re-adopted from its satellite
    after a proxy restart: a remote claude-code-cli session on a machine, by
    the same key the boot parks it on (``_session_machine``). Used by the
    shutdown path to LEAVE such a run running (so ``defer_orphaned_runs``
    parks it next boot) instead of failing + closing it."""
    chat = task_store.get_chat(chat_id) if chat_id else None
    if not chat:
        return False
    sid = chat.get("session_id") or ""
    machine = (_session_machine(chat, sid) if sid
               else placement.machine_of(chat.get("execution_target") or ""))
    return bool(machine) and _is_remote_cli_chat(chat)


async def sweep_expired() -> None:
    """Fail parked runs whose recovery deadline lapsed (satellite never
    reconnected). Runs on the reaper cadence."""
    now = time.monotonic()
    async with _lock:
        expired = [
            sid for sid, rec in _parked.items() if now > rec["deadline"]
        ]
        records = [_parked.pop(sid) for sid in expired]
    for rec in records:
        rid = rec.get("run_id")
        if rid:
            await _fail_and_report(rid, rec.get("chat_id") or "", RUN_MACHINE_NOT_BACK)
        logger.info(
            "Recovery: parked session %s deadline lapsed — failed run %s",
            (rec.get("chat_id") or "")[:16], rid or "(none)",
        )
    await cut_turns.tell_waiting()
    # Before the pass's sweep, which can hold this tick for minutes.
    from services.scheduler import stranded_runs
    try:
        await stranded_runs.sweep()
    except psycopg.Error:
        logger.warning("Recovery: the stranded sweep could not read the store", exc_info=True)
    from services.scheduler import continue_pass
    await continue_pass.sweep()


def _lane_state(run_id: str, chat_id: str) -> tuple[dict | None, bool, int, int]:
    """The run; whether the lane ran again (a newer run of its chat, or
    another round that opened on it: a live run with its session on its row;
    a follow-up that only waits at the lane gate runs after this one); and
    its chat's cursors: the row before the run
    started (from the newest rows: a run longer than them reports their span)
    and the last row now."""
    run = task_store.get_run(run_id)
    newest = (task_store.get_runs_for_chats([chat_id]).get(chat_id) or {}).get("id") or ""
    ran_again = bool(chat_id) and (
        newest != run_id
        or any(r["id"] != run_id and (r.get("session_id") or "")
               for r in task_store.live_delegate_rounds(chat_id)))
    rows = task_store.get_chat_messages(chat_id) if chat_id else []
    started = (run or {}).get("started_at") or ""
    before = [r["id"] for r in rows if (r.get("created_at") or "") < started]
    first = rows[0]["id"] - 1 if rows else 0
    return run, ran_again, (max(before) if before else first), (rows[-1]["id"] if rows else 0)


def _round_waits(run_id: str, chat_id: str) -> bool:
    """Another delegated round of the lane is live, opened or still at the
    lane gate: the delegator continued the lane. Synchronous."""
    return bool(chat_id) and any(
        r != run_id for r in task_store.live_delegate_run_ids_for_chat(chat_id))


def _lane_facts(run: dict, chat_id: str, cursor: int, end: int) -> dict[str, Any]:
    """The lane's facts a delegate result carries, as the runner passes them."""
    return {"worker_chat_id": chat_id, "output_cursor": cursor,
            "prompt_text": run.get("prompt_text") or "",
            "run_started_at": run.get("started_at") or "", "turn_end_cursor": end}


def _lane_chat(run: dict) -> str:
    """The chat a run's lane lives in: its own, else the one its task
    continues (a follow-up that never opened its round has none on its row).
    Synchronous."""
    chat_id = run.get("chat_id") or ""
    if chat_id:
        return chat_id
    dyn = task_store.get_dynamic_task(run.get("task_id") or "") or {}
    return dyn.get("target_chat_id") or ""


async def _fail_and_report(run_id: str, chat_id: str, reason: str, *, cause: str = "") -> None:
    """A parked run that will not come back fails with ``reason``, and its
    delegator hears that the restart cut the lane, as from a replay that
    never began (``_finalize_run``): the lane's round, collected as the
    runner collects it, under the restart's ending, so a lane with no report
    says so (``cause``: the record's, a machine's restart is named as one).
    A lane that ran again since, or that another round waits on (its
    delegator continued it: a second "continue it" would double the round),
    is reported to the caller's card alone, waking nobody. A run that never opened its round (no session on its row)
    says that it never started and names the lane it was sent to, with
    nothing collected: the lane's rows are other rounds'. Of two that end a
    run, only the one that failed it reports."""
    from core.events import turn_ending
    before = await asyncio.to_thread(task_store.get_run, run_id) or {}
    started = bool(before.get("session_id"))
    if not await asyncio.to_thread(task_store.fail_live_run, run_id,
                                   reason if started else RUN_NOT_STARTED):
        return
    if not started:
        ending = turn_ending.TurnEnding(reason=turn_ending.RESTARTED,
                                        detail=turn_ending.NOT_STARTED)
        lane = chat_id or await asyncio.to_thread(_lane_chat, before)
        await _report(before, run_status.FAILED, ending.line(), ending, lane_chat_id=lane)
        return
    run, ran_again, cursor, end = await asyncio.to_thread(_lane_state, run_id, chat_id)
    if run is None:
        return
    detail = turn_ending.MACHINE if cause == cut_records.CAUSE_MACHINE else ""
    ending = turn_ending.TurnEnding(reason=turn_ending.RESTARTED, detail=detail)
    if ran_again or await asyncio.to_thread(_round_waits, run_id, chat_id):
        # The lane goes on with a round its delegator queued: this one's card
        # is told it was cut, as cancelled (nothing failed: the run's own row
        # stays failed), and nobody is woken (a second "continue it" would
        # double the round).
        await _report(run, run_status.CANCELLED, CUT_ROUND_WENT_ON, ending, lane_chat_id=chat_id, wake=False)
        return
    await _report(run, run_status.FAILED, "", ending, **_lane_facts(run, chat_id, cursor, end))


async def report_stopped(run: dict) -> None:
    """The delegator's result of a run a person stopped where no round of it
    was open in this process: it waited for its replay or its reopen
    (``stranded_runs.stop``), or it was reopened and stopped before its round
    (``runner._end_unstamped``). A stopped run's words, with the lane's rows
    since the run started."""
    await _report_unopened(run, run_status.USER_INTERRUPTED, "was stopped by the user before it finished.")


async def report_failed(run: dict, why: str) -> None:
    """The delegator's result of a reopened run that failed before its round
    opened, or that the platform stopped there (``runner._end_unstamped``):
    a failed run's words with the reason, and the lane's rows since the run
    started."""
    await _report_unopened(run, run_status.FAILED, f"failed: {why}")


async def _report_unopened(run: dict, final: str, words: str) -> None:
    run_id, chat_id = run["id"], run.get("chat_id") or ""
    _run, _again, cursor, end = await asyncio.to_thread(_lane_state, run_id, chat_id)
    await _report(run, final, f'⚠ Delegated task "{await _task_name(run)}" {words}', None,
                  **_lane_facts(run, chat_id, cursor, end))


async def _task_name(run: dict) -> str:
    dyn = await asyncio.to_thread(task_store.get_dynamic_task, run.get("task_id") or "")
    return (dyn or {}).get("name") or run.get("task_id") or ""


def cut_reason(record: dict) -> str:
    """The run row's reason for a record nothing continues, by what cut it."""
    return RUN_CUT_MACHINE if record.get("cause") == cut_records.CAUSE_MACHINE else RUN_CUT


async def _report(run: dict, final: str, output: str, ending: TurnEnding | None,
                  **lane: Any) -> None:
    """The delegate hand-off of a run the recovery ended (best-effort;
    ``_deliver_task_result`` gates itself on ``on_complete_agent``)."""
    from services.scheduler import scheduler
    dyn = await asyncio.to_thread(task_store.get_dynamic_task, run.get("task_id") or "")
    if not dyn:
        return
    task = scheduler._row_to_task(dyn)
    try:
        await scheduler._deliver_task_result(task, final, output or "", run_id=run["id"],
                                             ending=ending, **lane)
    except Exception:
        logger.exception("Recovery delegate delivery failed for %s", run["id"])


def _placed_here(ctx, machine_id: str) -> bool:
    """A reloaded context belongs to the reporting machine: it names it, or
    it names none (an older index) while its placement is not a local one (a
    local context never matches an id a satellite reports)."""
    pl = getattr(ctx, "placement", None)
    placed_on = getattr(pl, "machine_id", "") or ""
    if placed_on:
        return placed_on == machine_id
    return getattr(pl, "kind", "") != placement.KIND_LOCAL


def _readopt_state(machine_id: str, session_id: str, chat: dict | None) -> tuple[str, int] | None:
    """The mode and token floor a reported session comes back with, or None
    when it must not come back. A session the platform closed on purpose (an
    idle reap, offboarding, a sign-out, a delete) lost its security context
    when it closed, and the index is written before the close returns, so a
    missing context is the record that it is closed; a context placed on
    another machine is that machine's session. The mode is the index's;
    an index written before modes were persisted falls back to the chat's
    (an unattended session runs ``auto``, a chat its row's mode). A session
    taken back in the same process (its record dropped at a reconnect after
    an expired grace) has no reloaded state: it keeps the mode and floor this
    process holds for it."""
    from core.session import session_kind
    from core.session.session_state import (
        get_session_client_type,
        get_session_security,
        security_context_expired,
    )
    ctx = get_session_security(session_id)
    if ctx is None or not _placed_here(ctx, machine_id):
        return None
    if security_context_expired(session_id):
        return None  # its process's token expired with it
    if _a_cut_call(machine_id, session_id, chat):
        return None  # its call ended: the person's next message builds the chat's session
    mode, floor = _kept_state(session_id)
    if not mode:
        if not session_kind.attended(get_session_client_type(session_id)):
            mode = "auto"
        else:
            mode = (chat or {}).get("permission_mode") or ASK_MODE
    return mode, floor


def _kept_state(session_id: str) -> tuple[str, int]:
    """The mode and token floor the security index kept for a session (the
    floor is the start of the token its process carries, 0 when unknown);
    a session taken back in this process keeps what this process holds."""
    from core.session.session_state import live_state, reloaded_state
    mode, floor = reloaded_state(session_id)
    if not mode and not floor:
        mode, floor = live_state(session_id)
    return mode, int(floor or 0)


def _a_cut_call(machine_id: str, session_id: str, chat: dict | None) -> bool:
    """A session in a chat a call wrote for a person (a Shared-only agent's
    shared chat included) that the index does not record as a chat's,
    placed on this machine, that no call socket of this process holds any
    more: a restart, or a socket that ended without its teardown, ended the
    call. The session index is written behind the security index, so a crash
    can leave a call's context with no kind beside it: only a session
    positively recorded as a chat's comes back. A cut call never does, so
    the person's next message builds the chat's own session on its
    conversation, and it is declined with its context. The agent's
    conversation comes back as before."""
    from core.session.session_state import get_session_client_type, get_session_security
    from core.session.visibility import is_phone_chat_owner
    from services.phone import call_chats
    if chat is None or is_phone_chat_owner(chat.get("user_sub")):
        return False
    if (session_kind.of_chat(chat) is not session_kind.PHONE
            or get_session_client_type(session_id) == session_kind.DASHBOARD.name):
        return False
    ctx = get_session_security(session_id)
    if ctx is None or not _placed_here(ctx, machine_id):
        return False
    return call_chats.held_by(chat.get("id") or "") != session_id


def _mark_reported_starting(reported: dict[str, dict], machine_id: str,
                            others: dict[str, dict] | None = None) -> None:
    from core.session.session_manager import capabilities_for_path
    from core.session.session_state import (
        READOPT_MARK_TTL_S,
        get_session_security,
        mark_starting,
    )
    for sid, info in [*reported.items(), *(others or {}).items()]:
        ctx = get_session_security(sid) if sid else None
        if ctx is None or not _placed_here(ctx, machine_id):
            continue
        try:
            caps = capabilities_for_path(info.get("execution_path") or "")
        except ValueError:
            continue
        if caps.runtime.supports_reattach_after_restart or caps.runtime.readopts_idle_session:
            mark_starting(sid, READOPT_MARK_TTL_S)


def _clear_readopt_mark(session_id: str, machine_id: str) -> None:
    """A reported session this recovery will not adopt: its re-adoption
    mark goes now, not at the end of its window. (A chat that already has a
    live pump keeps it: that pump's own start owns the id; and a mark set
    for the machine the context is placed on is that machine's.)"""
    from core.session.session_state import clear_starting, get_session_security
    ctx = get_session_security(session_id)
    if ctx is None or _placed_here(ctx, machine_id):
        clear_starting(session_id)


def lost_standing(machine_id: str, reported: dict[str, dict], *,
                  stops: bool = True) -> dict[str, str]:
    """``_lost_standing`` for a caller outside this module (the terminal
    claim, with ``stops`` False: a Stop never closes a terminal)."""
    return _lost_standing(machine_id, reported, stops=stops)


#: Why a reported session does not come back, by what the database holds.
LOST_ACCESS = "its person's access changed"
LOST_SIGNED_OUT = "its person signed out everywhere or changed the password"
LOST_STOPPED = cut_records.STOPPED_BY_PERSON


def _lost_standing(machine_id: str, reported: dict[str, dict], *,
                   stops: bool = True) -> dict[str, str]:
    """The reported sessions that may not come back, each with its cause,
    whatever the security index's file still says (a close whose removal a
    crash cut leaves its record there): the role the context started with
    outranks the person's role now, or the person lost the agent (the rule
    the offboarding closer applies to held sessions; these are not held
    yet); the person changed the password or signed out everywhere after the
    session's start (its token floor, ``_kept_state``); or, with ``stops``,
    a person's Stop closed it (the run's own session, its cut record settled
    by the Stop at or after the start). A start that is not known (a floor of
    0) is judged by the role alone. Synchronous: runs on the DB executor."""
    from core.session.session_state import get_session_security
    from services.agents import offboarding_sessions as offboard
    contexts = {}
    for sid in reported:
        ctx = get_session_security(sid) if sid else None
        if ctx is not None and _placed_here(ctx, machine_id) and not ctx.is_external:
            contexts[sid] = ctx
    if not contexts:
        return {}
    floors = {sid: _kept_state(sid)[1] for sid in contexts}
    lost: dict[str, str] = {}
    people = {c.username for c in contexts.values() if c.username}
    if people:
        from auth.providers import session_token_is_current
        rows, by_name = offboard._people(set(), people)
        for sid, ctx in contexts.items():
            if not ctx.username:
                continue
            sub = by_name.get(ctx.username, "")
            if offboard._outranked(ctx.role or "", offboard._role_now(rows, sub, ctx.agent), False):
                lost[sid] = LOST_ACCESS
                continue
            user = (rows.get(sub) or (None, {}))[0]
            if user and floors[sid] and not session_token_is_current(user, {"iat": floors[sid]}):
                lost[sid] = LOST_SIGNED_OUT
    if stops:
        from services.scheduler import stranded_runs
        for sid, record in cut_records.stopped_sessions(
                [sid for sid in contexts if sid not in lost and floors[sid]]).items():
            settled = _epoch_of(record.get("settled_at") or "")
            if settled is not None and settled >= floors[sid] \
                    and stranded_runs._is_the_runs_own_session(record, sid):
                lost[sid] = LOST_STOPPED
    return lost


def _epoch_of(iso: str) -> float | None:
    try:
        when = datetime.fromisoformat(iso)
    except ValueError:
        return None
    return (when if when.tzinfo else when.replace(tzinfo=timezone.utc)).timestamp()


async def on_sessions_alive(machine_id: str, sessions: list[dict],
                            other_sessions: list[dict] | None = None) -> None:
    """A satellite reported its headless sessions post-auth: ``sessions``,
    the live ones of the engines whose turn survives a proxy restart, and
    ``other_sessions`` (0.5.138; None when the frame did not carry it),
    every other one it holds.

    First the records of this machine whose stream an expired grace severed
    are dropped. Then: adopt each parked run whose session the satellite
    still holds (a parked run it does not name waits for ``on_alive_report``);
    adopt plain (non-run) chats with a live turn so their tail lands;
    take back an idle session the platform still has a chat and a context
    for, and close the rest on the satellite."""
    reported = {s.get("session_id", ""): s for s in sessions}
    others = {s.get("session_id", ""): s for s in (other_sessions or ())
              if s.get("session_id") and s.get("session_id") not in reported}

    # Before the first await: a session the satellite kept alive is live
    # again for the re-adoption window (its context survived the restart in
    # the security index; only an engine whose sessions are re-adopted). The
    # adoption paths clear the mark when they insert or decline.
    _mark_reported_starting(reported, machine_id, others)
    from core.remote import satellite_link
    from core.session.session_state import take_recover_pending
    # Every reported session's replay mark is read now, a parked one's too,
    # so none outlives this report.
    marked = {sid for sid in reported if take_recover_pending(sid)}
    # A turn the restart cut that its machine still holds is replayed even
    # when it finished while the platform was down: the satellite kept it.
    marked |= {sid for sid in reported if cut_turns.waiting(sid)}

    from core.session.session_manager import _get_remote_layer
    remote = _get_remote_layer()
    left_to_resume = await _drop_severed(remote, machine_id, reported, others)

    async with _lock:
        # Parked runs for THIS machine.
        mine = {
            sid: rec for sid, rec in _parked.items()
            if rec["machine_id"] == machine_id
        }

    # A session it does not name stays parked with its deadline: only a
    # complete report (``on_alive_report``) says it is gone.
    to_adopt: list[tuple[str, dict, dict]] = []
    for sid, rec in mine.items():
        info = reported.get(sid)
        if info is not None:
            to_adopt.append((sid, rec, info))

    async with _lock:
        for sid, _rec, _info in to_adopt:
            _parked.pop(sid, None)

    async def _decline(sid: str, why: str, *, drop_state: bool = False,
                       incarnation: str = "") -> None:
        """A reported session the platform does not take back: its mark goes
        and its process on the satellite ends, only the object the report
        named (``drop_state``: and the context the index kept for it, when it
        is this machine's, with the session's local caches)."""
        _clear_readopt_mark(sid, machine_id)
        logger.info("Recovery: closing %s on %s (%s)", sid[:8], machine_id[:8], why)
        if remote is not None:
            await remote.close_unadopted(machine_id, sid, incarnation)
        if drop_state and not _taken_meanwhile(sid):
            # After the machine is told: it ends the process on the word, so
            # a record a crash leaves behind finds nothing to take back.
            _drop_local_state(sid)

    # Adopt parked runs, unless the database says the session may not come back.
    lost_parked = (await asyncio.to_thread(
        _lost_standing, machine_id, {sid: info for sid, _rec, info in to_adopt}) if to_adopt else {})
    for sid, rec, info in to_adopt:
        state = _readopt_state(machine_id, sid, None)
        if state is None or sid in lost_parked:
            rid = rec.get("run_id")
            if rid:
                await _fail_and_report(rid, rec.get("chat_id") or "", RUN_SESSION_CLOSED)
            await _decline(sid, lost_parked.get(sid) or "no security context for its run",
                           drop_state=sid in lost_parked, incarnation=info.get("incarnation") or "")
            continue
        satellite_link.adoption_begins(machine_id, sid)
        spawn(_recover_session(
            machine_id, sid, info,
            run_id=rec.get("run_id"), chat_id=rec["chat_id"],
            agent=rec["agent"], state=state, cut=cut_turns.claim(sid),
            client_type=_client_type_of(None, rec.get("run_id") or "run"),
        ), name="recover_session", owner="run_recovery")

    # Adopt plain chats whose turn was live at the shutdown (no parked run)
    # so their output lands and the turn closes: a turn the satellite still
    # runs, and one the shutdown marked that finished while the proxy was
    # down (replayed from the satellite's retained turn). Skip anything
    # already live, being spawned or parked. A chat session whose person
    # lost the standing it started with is closed instead (a run is stopped
    # by its own path). Each step looks again at "held or spawning" right
    # before it acts: a person's start may have taken the id meanwhile.
    lost = await asyncio.to_thread(_lost_standing, machine_id, {**reported, **others})
    already = {sid for sid, _, _ in to_adopt}
    replayed: set[str] = set()
    for sid, info in reported.items():
        if sid in already or sid in _parked or sid in left_to_resume:
            continue
        if not info.get("turn_active") and sid not in marked:
            continue
        replayed.add(sid)
        if _held_or_spawning(remote, sid):
            _clear_readopt_mark(sid, machine_id)
            continue  # already re-registered, or a start owns it
        incarnation = info.get("incarnation") or ""
        if sid in lost:
            await _decline(sid, lost[sid], drop_state=True, incarnation=incarnation)
            continue
        chat = await asyncio.to_thread(task_store.get_chat_by_session, sid)
        if _held_or_spawning(remote, sid):
            continue
        if not chat:
            await _decline(sid, "no chat row",
                           drop_state=_readopt_state(machine_id, sid, None) is not None,
                           incarnation=incarnation)
            continue
        state = _readopt_state(machine_id, sid, chat)
        if state is None:
            await _decline(sid, "closed on purpose, placed on another machine, or a cut call",
                           drop_state=_a_cut_call(machine_id, sid, chat), incarnation=incarnation)
            continue
        satellite_link.adoption_begins(machine_id, sid)
        spawn(_recover_session(
            machine_id, sid, info,
            run_id=None, chat_id=chat["id"], agent=chat.get("agent") or "",
            state=state, cut=cut_turns.claim(sid), client_type=_client_type_of(chat, session_id=sid),
        ), name="recover_session", owner="run_recovery")

    # Idle sessions the satellite kept alive across the restart get
    # re-REGISTERED (registry only — nothing streams). Without this they
    # were invisible to the remote idle reaper and lived forever
    # satellite-side, eating the machine's session capacity until every
    # new spawn failed "at capacity" (hit live 2026-08-11 after an
    # evening of proxy redeploys). With a chat row they become normal
    # idle sessions again, in their own mode and with their own context —
    # resumable, reaped on the standard leash; a session with no chat row,
    # or one the platform closed on purpose, is closed on the satellite.
    if remote is not None:
        # The machine's file policy as it is now, for the sessions taken back
        # idle below (read once per report, off the loop).
        from storage import remote_store
        machine_row = await asyncio.to_thread(remote_store.get_remote_machine, machine_id)
        allow_full_fs = bool((machine_row or {}).get("allow_full_fs"))
        for sid, info in reported.items():
            if (sid in already or sid in replayed or sid in _parked
                    or sid in left_to_resume or info.get("turn_active")):
                continue
            if _held_or_spawning(remote, sid):
                continue
            incarnation = info.get("incarnation") or ""
            if sid in lost:
                await _decline(sid, lost[sid], drop_state=True, incarnation=incarnation)
                continue
            chat = await asyncio.to_thread(task_store.get_chat_by_session, sid)
            if _held_or_spawning(remote, sid):
                continue
            if not chat:
                await _decline(sid, "no chat row",
                               drop_state=_readopt_state(machine_id, sid, None) is not None,
                               incarnation=incarnation)
                continue
            state = _readopt_state(machine_id, sid, chat)
            if state is None:
                await _decline(sid, "closed on purpose, placed on another machine, or a cut call",
                               drop_state=_a_cut_call(machine_id, sid, chat), incarnation=incarnation)
                continue
            path = info.get("execution_path") or ""
            model = await asyncio.to_thread(_adopted_model, chat, path, info.get("model") or "")
            try:
                if await remote.adopt_idle_session(
                    machine_id=machine_id, session_id=sid,
                    agent_name=chat.get("agent") or info.get("agent_slug") or "",
                    # The satellite names the engine each live session runs
                    # (every satellite that reports sessions_alive does).
                    execution_path=path,
                    use_native_permissions=bool(
                        info.get("use_native_permissions")),
                    mode=state[0], token_floor=state[1],
                    # What the entry carries from 0.5.171: the thread, the
                    # model and the tools (empty from an older satellite).
                    resume_handle=info.get("resume_handle") or "", model=model,
                    used_mcps=info.get("mcp_servers") or (), allow_full_fs=allow_full_fs,
                    client_type=_client_type_of(chat, session_id=sid),
                ):
                    logger.info(
                        "Recovery: re-leashed idle session %s on %s (mode %s)",
                        sid[:8], machine_id[:8], state[0])
            except Exception:
                logger.exception(
                    "Recovery: idle adoption failed for %s on %s",
                    sid[:8], machine_id[:8])

        for sid, entry in others.items():
            if sid in left_to_resume:
                continue
            try:
                await _other_session(remote, machine_id, sid, entry, lost, _decline)
            except Exception:
                logger.exception("Recovery: the held session %s on %s failed",
                                 sid[:8], machine_id[:8])

    # This machine is back: redeliver any delegate wakes parked on ITS chats —
    # including idle ones the adopt loops above never touch (a wake stored
    # while the machine was away can only deliver now).
    async def _machine_wake_pass() -> None:
        from services.scheduler import scheduler as _sched
        try:
            held = remote.session_ids_on(machine_id) if remote is not None else []
            await _sched.redeliver_pending_wakes(machine_id=machine_id, session_ids=held)
        except Exception:
            logger.exception(
                "Wake redelivery failed for machine %s", machine_id[:8],
            )
    spawn(_machine_wake_pass(), name="machine_wake_pass", owner="run_recovery")
    spawn(_machine_resume_pass(machine_id, set(reported) | set(others)),
          name="machine_resume_pass", owner="run_recovery")


def _held_or_spawning(remote, session_id: str) -> bool:
    return remote is not None and (
        session_id in getattr(remote, "_sessions", {}) or remote.is_spawning(session_id))


def _taken_meanwhile(session_id: str) -> bool:
    """A start took the id while its machine was told: what is kept under the
    id is that start's. The Stop of a waiting run asks it too
    (``stranded_runs._stop_at_the_record``). Read with no await between it
    and the removal it guards."""
    from core.session import session_state
    return session_state.session_is_held(session_id) or session_state.is_starting(session_id)


def _drop_local_state(session_id: str) -> None:
    """A declined session's context and the platform-side caches kept for it
    (the caches are named by its context, so they go first)."""
    from core.session.session_state import (
        cleanup_session_permission_state,
        get_session_security,
    )
    try:
        from core.remote import remote_file_flow
        remote_file_flow.cleanup_session(session_id)
    except Exception:  # noqa: BLE001  # purging a recovered session's leftovers, the recovery goes on
        logger.warning("Recovery: remote file cache of %s not purged", session_id[:8],
                       exc_info=True)
    ctx = get_session_security(session_id)
    if ctx is not None:
        try:
            from services.mcp import mcp_output_relocation
            mcp_output_relocation.cleanup_session(session_id, ctx.agent, ctx.username)
        except Exception:  # noqa: BLE001  # purging a recovered session's leftovers, the recovery goes on
            logger.warning("Recovery: MCP outputs of %s not purged", session_id[:8],
                           exc_info=True)
    cleanup_session_permission_state(session_id)


async def _drop_severed(remote, machine_id: str, reported: dict[str, dict],
                        others: dict[str, dict]) -> set[str]:
    """The records of this machine whose stream was severed
    (``remote_stream_severed``: the record survives, its queue is not on the
    machine's current link), dropped: the record only, never the satellite's
    process, the context, mode, floor and binding kept. The report then takes
    an idle one back like any idle session. One the report names turn-active
    is returned: no replay (its turn ended when its queue was lost and was
    persisted then), its next turn resumes it."""
    left: set[str] = set()
    if remote is None:
        return left
    for sid in remote.session_ids_on(machine_id):
        if remote.is_spawning(sid) or not remote.remote_stream_severed(sid):
            continue
        try:
            await remote.prepare_resume(sid)
        except Exception:
            logger.exception("Recovery: severed record %s on %s not dropped",
                             sid[:8], machine_id[:8])
            continue
        entry = reported.get(sid) or others.get(sid)
        if entry is not None and entry.get("turn_active"):
            left.add(sid)
            _clear_readopt_mark(sid, machine_id)
        logger.info("Recovery: %s on %s lost its stream to an expired grace; its record "
                    "is dropped", sid[:8], machine_id[:8])
    return left


def _client_type_of(chat: dict | None, run_id: str | None = None, session_id: str = "") -> str:
    """The kind an adopted session is recorded under: a run's session is
    the scheduler's; a chat's session the kind the index kept for it (the
    person's own session in a chat a call wrote is a chat's), else its row's
    (a dashboard chat, a call). Recorded so a later record of the session
    (``continue_pass.session_gone``) reads one; before 2.0 an adopted
    session had none."""
    if run_id or session_kind.is_task_chat_id((chat or {}).get("id")):
        return session_kind.TASK.name
    from core.session.session_state import get_session_client_type
    return (session_id and get_session_client_type(session_id)) or session_kind.of_chat(chat).name


def _adopted_model(chat: dict, execution_path: str, reported: str) -> str:
    """The model an adopted session runs on: the chat's when its engine
    serves it, else the one the satellite reported. Synchronous: call it on
    the DB executor."""
    model = (chat or {}).get("model") or ""
    if model:
        try:
            from storage.billing import subscription_store
            if any((m.get("model_id") or "") == model
                   for m in subscription_store.list_models(execution_path)):
                return model
        except psycopg.Error:
            logger.debug("model check failed for %s", execution_path, exc_info=True)
    return reported or model


async def _other_session(remote, machine_id: str, sid: str, entry: dict,
                         lost: dict[str, str], decline) -> None:
    """One ``other_sessions`` entry: an idle session of an engine that takes
    one back, with a thread to resume, its chat on this engine and machine,
    its person's standing and its context, is taken back; anything else is
    closed on the satellite by the incarnation the report named."""
    from core.session.session_manager import (
        capabilities_for_path,
        resolve_execution_path,
    )
    if _held_or_spawning(remote, sid):
        return
    path = entry.get("execution_path") or ""
    try:
        takes_back = capabilities_for_path(path).runtime.readopts_idle_session
    except ValueError:
        takes_back = False
    why, drop, chat, state = "", False, None, None
    if not entry.get("alive"):
        why = "its process is not running"
    elif entry.get("turn_active"):
        why = "a turn nobody follows is running"
    elif not takes_back:
        why = f"{path or 'an unknown engine'} is not taken back"
    elif not entry.get("resume_handle"):
        why = "no thread to resume"
    elif sid in lost:
        why, drop = lost[sid], True
    else:
        chat = await asyncio.to_thread(task_store.get_chat_by_session, sid)
        pin = (chat or {}).get("execution_target") or ""
        from storage import remote_store
        machine = await asyncio.to_thread(remote_store.get_remote_machine, machine_id) if chat else None
        if not chat:
            why, drop = "no chat row", _readopt_state(machine_id, sid, None) is not None
        elif resolve_execution_path(chat.get("agent") or "",
                                    chat.get("execution_path") or "") != path:
            why = "its chat runs another engine"
        elif pin and not placement.runs_on(pin, machine_id,
                                           platform_machine=placement.machine_is_compute(machine)):
            why = "its chat runs on another machine"
        else:
            state = _readopt_state(machine_id, sid, chat)
            if state is None:
                why = "closed on purpose, placed on another machine, or a cut call"
                drop = _a_cut_call(machine_id, sid, chat)
    if why:
        if not _held_or_spawning(remote, sid):
            await decline(sid, why, drop_state=drop, incarnation=entry.get("incarnation") or "")
        return
    chat, state = cast("dict", chat), cast("tuple[str, int]", state)  # no why: the last branch set both
    model = await asyncio.to_thread(_adopted_model, chat, path, entry.get("model") or "")
    if await remote.adopt_idle_session(
        machine_id=machine_id, session_id=sid,
        agent_name=chat.get("agent") or entry.get("agent_slug") or "",
        execution_path=path,
        use_native_permissions=bool(entry.get("use_native_permissions")),
        mode=state[0], token_floor=state[1],
        resume_handle=entry.get("resume_handle") or "", model=model,
        used_mcps=entry.get("mcp_servers") or (),
        allow_full_fs=bool((machine or {}).get("allow_full_fs")),
        client_type=_client_type_of(chat, session_id=sid),
    ):
        logger.info("Recovery: took back the idle %s session %s on %s (mode %s)",
                    path, sid[:8], machine_id[:8], state[0])


async def _machine_resume_pass(machine_id: str, reported: set[str]) -> None:
    """The machine is back: what its blip left waiting resumes, beside the
    wake replay (which waits on every replayed turn). A background monitor
    rides out the grace, and one that outlived it has exited (the registries
    keep the still-running jobs): monitors are armed again for each session
    the satellite reported with its stream attached whose chat takes the
    chat contract (an ordinary chat, a worker or task chat outside its run's
    report). And the chats on the machine holding queued messages get their
    delivery armed: a start that fell in the gap deferred them, and nothing
    else would send them before the chat's next turn ends; their chips stop
    saying the machine is reconnecting."""
    from core.events import input_queue
    from core.events.pump_bg_monitors import arm_bg_monitors
    from core.session import background_leash
    from core.session.session_manager import _get_remote_layer
    from services.scheduler import lanes
    remote = _get_remote_layer()
    on_machine: list[str] = []
    try:
        if remote is not None:
            on_machine = remote.session_ids_on(machine_id)
        for sid in on_machine:
            if (sid not in reported or remote.remote_stream_severed(sid)
                    or not background_leash.background_pending_count(sid)):
                continue
            chat = await asyncio.to_thread(task_store.get_chat_by_session, sid)
            if lanes.takes_chat_contract(chat, sid):
                arm_bg_monitors(remote, sid, cast("dict", chat)["id"])  # the contract needs a chat row
    except Exception:
        logger.exception("Resume pass: monitors failed for machine %s", machine_id[:8])
    try:
        for chat_id in await asyncio.to_thread(
                task_store.list_chats_with_waiting_input, machine_id, on_machine):
            input_queue.clear_waiting(chat_id)
            # A message that waited on this machine's unanswered probe before
            # it dropped waits for nothing now either.
            input_queue.clear_waiting(chat_id, input_queue.WAITING_MACHINE)
            input_queue.arm(chat_id)
    except Exception:
        logger.exception("Resume pass: queued messages failed for machine %s",
                         machine_id[:8])


async def _recover_session(
    machine_id: str, session_id: str, info: dict,
    *, run_id: str | None, chat_id: str, agent: str,
    state: tuple[str, int] = ("", 0), cut: dict | None = None,
    client_type: str = "",
) -> None:
    """Drive the adopted turn through a recovery pump, then finalize the run.
    ``cut``: the turn's waiting closing-row entry (``cut_turns.claim``), held
    by this adoption until ``replay_settled``. Its open-turn record is taken
    before the replay pump writes its own, and the replay's first event
    decides (``_settle_cut``). A parked run whose turn is not adopted after
    all (the chat streams already, no remote layer) was taken out of the
    parked set for this adoption: nothing else would end it, so it fails here
    with its report. So does one whose adoption meets an exception while it
    still holds its claim: the claim is closed with its card, never left
    held. A claim already handed back is the pass's, and so is its run. While
    this runs, the run is driven (``_driving``): the stranded sweep leaves it
    alone."""
    if run_id:
        _driving.add(run_id)
    try:
        await _recover_session_turn(machine_id, session_id, info, run_id=run_id,
                                    chat_id=chat_id, agent=agent, state=state, cut=cut,
                                    client_type=client_type)
    except Exception:
        logger.exception("Recovery: the adoption of %s on %s failed", session_id[:8],
                         machine_id[:8])
        if cut_turns.holds(cut):
            await cut_turns.close_claimed(cut)
        if run_id and not (cut or {}).get("handed_back"):
            await _fail_and_report(run_id, chat_id, RUN_SESSION_CLOSED)
    finally:
        if run_id:
            _driving.discard(run_id)
        # Off its way to a binding, whatever it reached (``adopt_session``
        # says so itself once it restored one).
        from core.remote import satellite_link
        satellite_link.adoption_ends(machine_id, session_id)


async def _recover_session_turn(
    machine_id: str, session_id: str, info: dict,
    *, run_id: str | None, chat_id: str, agent: str,
    state: tuple[str, int], cut: dict | None, client_type: str,
) -> None:
    from core.events.stream_pump import ChatStreamPump, _active_pumps
    from core.session.session_manager import _get_remote_layer

    # Never double-drive a chat already streaming (a user's warmup may have
    # re-registered it while we were dispatching).
    existing = _active_pumps.get(chat_id)
    if existing is not None and not existing.is_done:
        logger.info(
            "Recovery: chat %s already has a live pump — skipping adopt",
            chat_id[:16],
        )
        await cut_turns.close_claimed(cut)
        if run_id:
            await _fail_and_report(run_id, chat_id, RUN_SESSION_CLOSED)
        return

    layer = _get_remote_layer()
    if layer is None:
        _clear_readopt_mark(session_id, machine_id)
        await cut_turns.close_claimed(cut)
        if run_id:
            await _fail_and_report(run_id, chat_id, RUN_SESSION_CLOSED)
        return
    taken = await _take_cut(chat_id, cut)
    if run_id and not await _run_is_live(run_id):
        # A person stopped the run after this adoption claimed its turn
        # (``stranded_runs.stop``). The Stop left the session to the adoption
        # in flight, so the adoption declines it: the process on the machine
        # ends (only the object the report named) and nothing is replayed.
        # The engine does not go on for the rest of a turn nobody wants.
        logger.info("Recovery: run %s is no longer live, closing %s on %s instead of "
                    "adopting its turn", run_id, session_id[:8], machine_id[:8])
        _clear_readopt_mark(session_id, machine_id)
        await cut_turns.close_claimed(cut)
        await layer.close_unadopted(machine_id, session_id, info.get("incarnation") or "")
        # After the machine is told, as a declined report's (``_decline``).
        if not _taken_meanwhile(session_id):
            _drop_local_state(session_id)
        return
    closed = False      # the replay never began: the cut turn got its closing row
    began = False       # the machine still held the turn: the replay writes it

    command_id = info.get("command_id", "")
    use_native = bool(info.get("use_native_permissions"))
    event_queue: asyncio.Queue = asyncio.Queue()

    async def _produce():
        from core.events.common_events import (
            DONE,
            ERROR,
            PRODUCER_DONE,
            TEXT,
            CommonEvent,
        )
        from core.remote.remote_resume import REPLAY_BEGIN
        nonlocal closed, began
        settled = False
        try:
            async for event in layer.adopt_session(
                machine_id=machine_id, session_id=session_id,
                agent_name=agent, command_id=command_id,
                execution_path=info.get("execution_path") or "",
                use_native_permissions=use_native,
                mode=state[0], token_floor=state[1],
                resume_handle=info.get("resume_handle") or "",
                model=info.get("model") or "",
                used_mcps=info.get("mcp_servers") or (),
                client_type=client_type,
            ):
                if not settled:
                    settled = True
                    began = event.type not in (DONE, ERROR)
                    truncated = (bool(event.data.get("truncated"))
                                 if event.type == REPLAY_BEGIN else False)
                    closed = await _settle_cut(chat_id, taken, began=began,
                                      truncated=truncated,
                                      errored=event.type == ERROR)
                    if truncated:
                        # The rows saved before the cut stay: the replay
                        # writes only what they do not hold, and its mark only
                        # when rows are missing between the two. With no such
                        # rows the mark is the turn's first block, as before.
                        marker = str(event.data.get("marker") or "")
                        overlap = await _kept_rows(chat_id, taken, marker)
                        if overlap is not None:
                            pump.replay_overlap = overlap
                        elif marker:
                            await event_queue.put(CommonEvent(type=TEXT, data={"content": marker}))
                if event.type != REPLAY_BEGIN:
                    await event_queue.put(event)
        except Exception as e:  # the producer turns any failure into an error event for the pump
            logger.exception("Recovery producer failed for %s", session_id[:8])
            settled = True
            await event_queue.put(CommonEvent(type=ERROR,
                                              data={"message": str(e)}))
        finally:
            if not settled:
                closed = await _settle_cut(chat_id, taken, began=False, truncated=False,
                                           errored=False)
            await event_queue.put(CommonEvent(type=PRODUCER_DONE, data={}))

    # A re-adopted run's usage goes to whoever pays, as its own pump's: read
    # before the producer starts, so no await falls between its start and
    # the pump's registration.
    from core.config import spender
    scope = await asyncio.to_thread(spender.run_bucket, run_id) if run_id else "user"
    producer = asyncio.create_task(_produce())
    from core.session.session_state import get_permission_queue
    pump = ChatStreamPump(
        chat_id=chat_id,
        session_id=session_id,
        producer=producer,
        event_queue=event_queue,
        perm_queue=get_permission_queue(session_id),
        scope=scope,
        source_type=(session_kind.TASK if run_id else session_kind.DASHBOARD).source_type,
    )
    # The replayed turn is the run's: a machine that loses the session under
    # it leaves the run to the pass by this name. No ``TaskRun`` follows this
    # pump, so a limit keeps its card (there is nobody to take its round).
    pump.run_id = run_id or ""
    pump.limit_round_taken = bool(run_id)
    # The holder's own pump: a machine that cuts it is read from it.
    cut_turns.drives(cut, pump)
    _active_pumps[chat_id] = pump
    pump.start()
    logger.info(
        "Recovery: adopting turn for chat=%s session=%s run=%s",
        chat_id[:16], session_id[:8], run_id or "(none)",
    )
    # asyncio.wait, not the task: cancelling this coroutine at shutdown must
    # never cancel the adopted pump (its chat stays recoverable).
    await asyncio.wait({cast("asyncio.Task", pump._task)})  # start() set it
    if not cast("asyncio.Task", pump._task).cancelled() and cast("asyncio.Task", pump._task).exception() is not None:
        logger.error("Recovery pump error for chat %s", chat_id[:16],
                     exc_info=cast("asyncio.Task", pump._task).exception())

    await cut_turns.replay_settled(cut, began=began)
    if run_id:
        await _finalize_run(run_id, chat_id, cut_short=closed, pump=pump)


async def _run_is_live(run_id: str) -> bool:
    run = await asyncio.to_thread(task_store.get_run, run_id)
    return run is not None and run_status.is_live(run.get("status"))


async def _take_cut(chat_id: str, cut: dict | None) -> open_turns.Taken | None:
    """The claimed cut turn's record, taken on the chat's lane before the
    replay pump writes its own: its early ranges and its start, None when
    there is none."""
    if cut is None:
        return None
    from core.events import chat_writer
    try:
        return await chat_writer.submit(chat_id, functools.partial(
            open_turns.take_record, chat_id, cut["token"]), label="turn_replay_take")
    except Exception:  # the replay goes on: at worst it repeats rows a checkpoint saved
        logger.exception("Recovery: chat %s: the cut turn's record was not taken", chat_id[:16])
        return None


async def _kept_rows(chat_id: str, taken: open_turns.Taken | None, marker: str):
    """The walk a truncated replay's pump saves through
    (``core/events/replay_overlap.ReplayOverlap``): the rows the cut turn
    saved before the cut, read on the chat's lane. None when the turn saved
    none (the record was not taken, no checkpoint ran) or they could not be
    read: the replay then writes all it has, as before."""
    if taken is None or not taken.early:
        return None
    from core.events import chat_writer
    from core.events.replay_overlap import ReplayOverlap
    try:
        rows = await chat_writer.submit(chat_id, functools.partial(
            task_store.chat_rows_in_ranges, chat_id, taken.early), label="turn_replay_early")
    except Exception:  # the replay goes on: at worst it repeats rows a checkpoint saved
        logger.exception("Recovery: chat %s: the rows saved before the cut were not read",
                         chat_id[:16])
        return None
    if not rows:
        return None
    logger.info("Recovery: chat %s: a truncated replay, %d row(s) saved before the cut stay",
                chat_id[:16], len(rows))
    return ReplayOverlap(rows, marker)


async def _settle_cut(chat_id: str, taken: open_turns.Taken | None, *,
                      began: bool, truncated: bool, errored: bool) -> bool:
    """The replay's first event decides the cut turn's early rows, before the
    replay writes anything: an untruncated replay writes the whole turn, so
    they go; a truncated one lost the turn's start, so they stay, and the
    replay writes only what they do not hold (``_kept_rows``); no replay at
    all keeps them and writes the
    closing row (an error's own card ends the turn instead). True when it
    wrote the closing row."""
    if taken is None:
        return False
    from core.events import chat_writer
    try:
        if began and not truncated and taken.early:
            dropped = await chat_writer.submit(chat_id, functools.partial(
                task_store.delete_chat_message_ranges, chat_id, taken.early),
                label="turn_replay_drop")
            logger.info("Recovery: chat %s: %s row(s) saved early left to the replay",
                        chat_id[:16], dropped)
        elif not began and not errored:
            await cut_turns.close_taken(chat_id, taken.started_at)
            return True
    except Exception:  # the replay goes on
        logger.exception("Recovery: chat %s: the cut turn's early rows were not settled", chat_id[:16])
    return False


async def _finalize_run(run_id: str, chat_id: str, *, cut_short: bool = False,
                        pump=None) -> None:
    """Complete a recovered run: persist the collected output + cost, then
    fire the delegate callback if any. Called after the recovery pump ends.
    ``cut_short``: the replay never began and the chat got the restart's
    closing row: the run fails with the same ending, its callback says so.
    ``pump``: the replay's. A turn its machine cut (the machine came back
    without the session) is no verdict: the run is left as it is for the
    continue pass, which reopens it (the replay's end handed its record back,
    ``cut_turns.replay_settled``). A replayed turn that ended with a typed
    ending fails the run with it, as a live run's turn does."""
    from core.events import turn_ending
    from services.scheduler import scheduler
    if pump is not None and getattr(pump, "cut_by_machine", False):
        logger.info("Recovery: run %s left for the continue pass, its machine cut the "
                    "replayed turn", run_id)
        return
    ending = turn_ending.TurnEnding(reason=turn_ending.RESTARTED) if cut_short else None
    if ending is None and pump is not None:
        typed = getattr(pump, "last_ending", None)
        if typed is not None and typed.reason != turn_ending.STOPPED:
            ending = typed
    final = run_status.FAILED if ending else run_status.COMPLETED
    run = await asyncio.to_thread(task_store.get_run, run_id)
    if not run or not run_status.is_live(run.get("status")):
        return  # already terminal (a concurrent path finished it)
    _run, _again, cursor, end = await asyncio.to_thread(_lane_state, run_id, chat_id)
    output = await asyncio.to_thread(scheduler._collect_task_output, chat_id, cursor)
    chat_row = await asyncio.to_thread(task_store.get_chat, chat_id) or {}
    cost = chat_row.get("total_cost") or 0
    await asyncio.to_thread(
        task_store.update_run, run_id, status=final,
        output_text=output[:10000] if output else "",
        completed_at=_now(),
        cost_usd=cost if cost > 0 else None,
        chat_id=chat_id,
        error_message=ending.summary() if ending else None,
    )
    logger.info("Recovery: run %s %s after re-adopt", run_id, final)
    await _report({**run, "id": run_id}, final, output, ending,
                  **_lane_facts(run, chat_id, cursor, end))


async def reopen_run(run: dict, record: dict) -> bool | None:
    """Reopen a run the platform cut, under its own id (the continue pass
    calls this for the run's record): a continue round on the run's own
    session, built by the task builder with the run's identity, whose
    verdict, checks, end page and delivery to the delegator are the run's
    own. True when the run was started again, None when a person stopped it
    meanwhile (the record was no longer this reopen's to take: nothing is
    done, no card). A run that cannot be (its
    task row is gone, its lane ran again, the task runs already) fails with
    the restart's reason, and its delegator is told as from any run the
    recovery fails (``_fail_and_report``), in the words of what cut it."""
    from services.scheduler import continue_pass, runner, scheduler
    run_id, chat_id = run["id"], run.get("chat_id") or record.get("chat_id") or ""
    reason, cause = cut_reason(record), record.get("cause") or ""
    # The cut run's own task may still be unwinding (a machine cut it while
    # the platform ran): it holds the task's reservation until it ends.
    await runner.unwound(run_id)
    dyn = await asyncio.to_thread(task_store.get_dynamic_task, run.get("task_id") or "")
    chat = await asyncio.to_thread(task_store.get_chat, chat_id) if chat_id else None
    _run, ran_again, _cursor, _end = await asyncio.to_thread(_lane_state, run_id, chat_id)
    if not dyn or not chat or ran_again:
        await _fail_and_report(run_id, chat_id, reason, cause=cause)
        return False
    text = await asyncio.to_thread(continue_pass.words, record, chat)
    # The record is settled by whoever takes the run: this reopen, or a
    # person's Stop (``stranded_runs.stop``). One statement, one winner, and no
    # await between it and the launch.
    if record.get("id") and not await cut_turns.taken_for_reopen(record):
        logger.info("Recovery: run %s is not reopened, its record was settled meanwhile", run_id)
        return None
    if not runner.reopen(run, scheduler._row_to_task(dyn), text):
        await _fail_and_report(run_id, chat_id, reason, cause=cause)
        return False
    logger.info("Recovery: run %s reopened on chat %s", run_id, chat_id[:16])
    return True


async def start_pending_runs() -> int:
    """The runs left ``pending`` with no start: a fire the platform held for
    a planned stop, or one that waited for its slot when the process ended.
    Each starts under its own id with its own prompt (boot, after the
    continue pass, and the drain's resume). Returns how many started."""
    from core import drain
    from services.scheduler import runner, scheduler, shared
    started = 0
    for run in await asyncio.to_thread(task_store.list_orphaned_runs):
        if (run.get("status") != run_status.PENDING or run["id"] in shared._running_tasks
                or not reopens(run) or _refused_by_a_boot(run) or drain.holding()):
            continue
        dyn = await asyncio.to_thread(task_store.get_dynamic_task, run.get("task_id") or "")
        if not dyn:
            await asyncio.to_thread(task_store.fail_live_run, run["id"], "The task was removed before its run started")
            continue
        if not dyn.get("enabled", True):
            continue
        # The reservation its fire kept through the hold.
        if shared._active_task_ids.get(dyn["id"]) == run["id"]:
            shared._active_task_ids.pop(dyn["id"], None)
        if runner.reopen(run, scheduler._row_to_task(dyn), run.get("prompt_text") or "", fresh=True):
            started += 1
    if started:
        logger.info("Recovery: started %d run(s) that waited for the platform", started)
    return started
