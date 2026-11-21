"""Delegate result delivery and continuation wakes: the persistent and one-shot
delivery paths, the echo turn, wake redelivery after a restart and the
interactive re-warm.

One piece of the task scheduler; ``services/scheduler/scheduler.py`` is the
facade that assembles them and holds the registry API. Every ``core.*`` and
service import stays function-local so the standalone scheduler can import
this package without the platform.
"""

import asyncio
import contextlib
import functools
import json
import logging
import uuid
from datetime import datetime, timezone
from typing import TYPE_CHECKING

import psycopg

import config
from auth import roles
from core import drain, placement
from core.background import spawn
from core.events import turn_ending
from core.execution_layer import ResumeVerdict
from core.session import chat_rebuild
from core.session.resume_probe import RESUME_PROBE_BUDGET_BACKGROUND_S, settle_resume
from core.session.visibility import (
    is_shared_chat_owner,
    is_synthetic_owner,
    is_task_chat_owner,
)
from services.scheduler import lanes, shared, wake_fresh
from services.scheduler.delivery_identity import (
    _shared_chat_person,
    _standing_of,
    _wake_refused,
    _wake_visibility,
    resolve_delivery_person,
)
from services.scheduler.lanes import _run_echo_turn_pumped
from services.scheduler.wake_session import (  # noqa: F401
    _arm_chat_queue,
    _chat_pin,
    _release_wake_slot,
    _rewarm_interactive_and_wake,
    _role_after_wait,
    _spawn_wake_session,
    _wait_turn_open,
    _wake_superseded,
)
from storage import database as task_store
from storage.automation import run_status
from ws import wire_events as wire

if TYPE_CHECKING:

    from core.session.session_delivery import DeliveryOutcome

logger = logging.getLogger("otodock.scheduler")

# Chats one wake sweep delivers at once: a wake that waits for a slot on a
# full box holds up only its own chat.
_SWEEP_CONCURRENCY = 4


async def _store_undelivered_wakes(chat_id: str, wakes: list[str], *, person: str = "",
                                   role: str = "", by: str = "") -> bool:
    """Every rung failed: keep the wakes on the chat for its next warmup or
    turn, each naming the person and role it runs as (``by``: the creator
    whose role a wake with no person runs at). A live terminal on the chat
    (a person opened it while the wake waited) takes them at once, when it
    may (``spender.serves_lane``: never a viewer's terminal on a lane's
    chat). A lane whose person cannot run it any more keeps none: no start
    would ever take it (the result's own row is on the chat). True when
    stored."""
    from core.config import spender
    from core.session import interactive_session
    from storage.pg import run_db

    paid = await run_db(spender.round_spender, chat_id)
    if paid is not None and paid.refused:
        logger.info("Wake not stored on chat %s: %s", chat_id[:8], paid.refused)
        return False
    stored = False
    for wake in wakes:
        stored = bool(await run_db(functools.partial(
            task_store.append_pending_delegate_wake, chat_id, wake,
            person=person, role=role, by=by))) or stored
    if not stored:
        return False
    from core.events.stream_pump import note_stored_wake
    note_stored_wake(chat_id)
    isess = interactive_session.find_live_for_chat(chat_id)
    if isess is not None and isess.alive and await run_db(functools.partial(
            spender.serves_lane, chat_id, isess.session_id, built_for=isess.payer)):
        for rec in await run_db(task_store.claim_pending_wake_records, chat_id):
            if isess.queue_prompt(rec["prompt"], "delegate_result_replay", chat_id=chat_id,
                                  note=True) is None:
                await run_db(functools.partial(
                    task_store.append_pending_delegate_wake, chat_id, rec["prompt"],
                    person=rec["person"], role=rec["role"], by=rec["by"]))
    return True


def replay_after_release(session_id: str) -> None:
    """A session's seat was released (``pool_bindings.release_subscription``,
    the one release that popped its binding): a wake stored on its chat
    while it served another person may start the lane's own session now.
    Called on the loop and from threads: handed to the loop
    (``token_fanout.on_loop``) as a background task of its own, never
    awaited by the close. Nothing is read while no wake is stored."""
    from core.events import stream_pump
    if not session_id or not stream_pump.stored_wakes_noted():
        return
    from services.engines import token_fanout
    token_fanout.on_loop(functools.partial(_spawn_release_replay, session_id))


def _spawn_release_replay(session_id: str) -> None:
    from core import background
    background.spawn(_replay_after_release(session_id), name=session_id[:8], owner="release-replay")


async def _replay_after_release(session_id: str) -> None:
    from core.events import stream_pump
    from storage.pg import run_db
    try:
        chat = await run_db(_chat_after_release, session_id)
        if chat and stream_pump.take_stored_wake(chat["id"]):
            await _redeliver_chat(chat)
    except Exception:
        logger.exception("session=%s: the stored wakes' replay after its release failed", session_id[:8])


def _chat_after_release(session_id: str) -> dict | None:
    """The chat of a released session, read once the pool's queued writes
    landed: its binding row is gone, so the gate reads no stale payer.
    Synchronous: the DB executor."""
    from services.engines import subscription_pool
    subscription_pool.flush_binding_writes()
    return task_store.get_chat_by_session(session_id)


async def _deliver_task_result(task: shared.TaskDefinition, final_status: str, output_text: str,
                               *, worker_chat_id: str = "", output_cursor: int = 0,
                               prompt_row_id: int = 0, prompt_text: str = "",
                               run_started_at: str = "", ending=None,
                               run_id: str = "", background_pending: int = 0,
                               cut_short: str = "", turn_end_cursor: int = 0,
                               lane_chat_id: str = "", wake: bool = True) -> None:
    """Deliver completed task result to originating session/chat.

    The session id is the route key (it matches the in-memory notify-queue /
    pump tables). ``on_complete_chat_id`` is the persistent anchor that
    survives browser close / proxy restart: with a delegating chat the key is
    that chat's current session (``chats.session_id``), and the row's
    ``on_complete_session_id`` only when the chat is gone or has none. A
    delivery with no chat routes by the row's id.

    ``worker_chat_id`` (the run's own chat) turns on LANE FINALIZATION inside
    the spawned delivery task — after the run releases its RAM slot: await
    lane quiescence (a user may still be steering the worker), re-check the
    chat's abort flag, and re-collect cursor-based so the callback carries the
    complete round including ``[User interjected]`` rows. It also feeds the
    ``{{chat_id}}`` template token. ``run_started_at`` bounds the check
    verdict a failed run carries to this run (a worker chat is reused).
    ``ending`` (``core.events.turn_ending.TurnEnding``) is the turn's typed
    ending when the engine stopped it: the callback names it and the next step.
    ``run_id`` names the run whose attached files the result names
    (``services/delegation/result_files``): read after the lane settled,
    never copied here; a second delivery of the run names the same rows.
    ``background_pending`` counts the background work still running when the
    run ended: the callback says its results are not in the report.
    ``cut_short`` is the run's own end before the worker's report (a
    ``turn_ending`` word the runner decides; a typed ending other than a
    Stop counts when it is empty): with no file, no ``Blocked on:`` line and
    no message a person wrote after the turn ended (``turn_end_cursor``, the
    worker chat's last row then) the result is flagged ``report_missing`` and
    its callback opens with what to do (``lanes.report_missing``).
    ``lane_chat_id`` names the lane's chat for a run that wrote nothing
    there (a follow-up that never started, a run cancelled before its round
    opened): the callback, the event and the notes name it, and nothing of
    the lane is collected. Every result names its chat one way or the other.
    ``wake`` False writes the result on the delegating chat and wakes
    nobody, whether or not that chat has a session (a cut round whose lane
    goes on with another).
    """
    row = await asyncio.to_thread(task_store.get_dynamic_task, task.id)
    on_complete_agent = (row.get("on_complete_agent") if row else None) or task.on_complete_agent
    on_complete_prompt = (row.get("on_complete_prompt") if row else None) or task.on_complete_prompt
    on_complete_session_id = (row.get("on_complete_session_id") if row else None) or task.on_complete_session_id
    on_complete_chat_id = (row.get("on_complete_chat_id") if row else None) if row else None

    if not on_complete_agent or not on_complete_prompt:
        logger.info(
            "Task result delivery skipped (no on_complete): task=%s, "
            "agent=%s, prompt=%s, "
            "session=%s, chat=%s, "
            "row=%s",
            task.id, on_complete_agent, 'yes' if on_complete_prompt else 'no',
            on_complete_session_id, on_complete_chat_id, 'found' if row else 'missing'
        )
        return

    # The delegating chat's current session is the one the ladder is asked
    # about: the id on the task row goes stale when the chat's session is
    # replaced. The row's id stands for a chat that is gone or has none.
    if on_complete_chat_id:
        chat = await asyncio.to_thread(task_store.get_chat, on_complete_chat_id)
        if chat and chat.get("session_id"):
            on_complete_session_id = chat["session_id"]

    if not on_complete_session_id and (wake or not on_complete_chat_id):
        logger.info("Task result delivery skipped (no session): task=%s", task.id)
        return

    logger.info(
        "Task result delivery starting: task=%s, "
        "session=%s, chat=%s",
        task.id, (on_complete_session_id or '-')[:8], on_complete_chat_id or 'none'
    )

    async def _finalize_and_deliver() -> None:
        status, output = final_status, output_text
        if worker_chat_id:
            # Finalization is best-effort: a failure here must never strand the
            # delegating agent — fall through and deliver what the run already
            # collected.
            try:
                # A user interrupt DEFERS the callback instead of racing the
                # user: the worker serves them directly in its own chat, and
                # the orchestrator gets ONE callback when the lane goes
                # genuinely quiet — carrying the interjections AND the
                # worker's replies. Normal terminals keep the short settle
                # (fire-and-forget pays no latency).
                if status == run_status.USER_INTERRUPTED:
                    await lanes._await_lane_quiescence(
                        worker_chat_id,
                        ceiling_seconds=1800.0,
                        settle_seconds=120.0,
                        immediate_quiet_ok=False,
                    )
                else:
                    await lanes._await_lane_quiescence(
                        worker_chat_id, ceiling_seconds=1800.0,
                    )
                chat = await asyncio.to_thread(task_store.get_chat, worker_chat_id)
                # A run the engine ended (a typed ending) stays failed: the
                # abort flag it also stamps is for the next turn's context.
                if (chat and chat.get("last_turn_aborted")
                        and status != run_status.FAILED):
                    status = run_status.USER_INTERRUPTED
                collected = await asyncio.to_thread(
                    lanes._collect_lane_output_since, worker_chat_id,
                    output_cursor, prompt_row_id, prompt_text,
                )
                if collected:
                    output = collected
            except Exception:
                logger.exception(
                    "Lane finalization failed for chat %s — "
                    "delivering the run's own collection", worker_chat_id[:8]
                )
            finally:
                lanes.release_report(worker_chat_id)
        # A worker that failed a check (CHECKS.md): the parent receives the
        # final failing result WITH the verdict, never an unchecked one.
        verdict = None
        if status == run_status.FAILED and worker_chat_id:
            try:
                from services.checks import evaluator as _checks
                verdict = await asyncio.to_thread(
                    _checks.latest_failure, worker_chat_id, run_started_at)
            except psycopg.Error:
                logger.debug("delegate verdict lookup failed", exc_info=True)
        # The files the worker attached; a store error names none.
        files: list = []
        files_skipped: list = []
        files_note = ""
        if run_id:
            try:
                from services.delegation import result_files as _result_files
                from storage.pg import run_db as _run_db
                # The run's attach lock: a copy still in flight records first.
                async with _result_files.run_lock(run_id):
                    files, files_skipped, files_note = await _run_db(_result_files.delivery_lists, run_id)
            except Exception:
                logger.exception("Result files lookup failed for run %s", run_id)
        # The chat a caller continues: the lane's, collected or only named.
        named = worker_chat_id or lane_chat_id
        # A follow-up that never started has no last message to mistake for
        # its outcome: its ending's own note says what to do.
        cut = cut_short or (ending.reason if ending is not None
                            and ending.reason != turn_ending.STOPPED
                            and ending.detail != turn_ending.NOT_STARTED else "")
        continued = False
        if cut and worker_chat_id and turn_end_cursor:
            try:
                continued = await asyncio.to_thread(
                    lanes.user_rows_after, worker_chat_id, turn_end_cursor)
            except psycopg.Error:
                logger.debug("the lane's rows after the turn could not be read", exc_info=True)
        missing = lanes.report_missing(cut, status, output, files, files_skipped,
                                       continued=continued)
        result_prompt = (on_complete_prompt
            .replace("{{output}}", output or "")
            .replace("{{task_id}}", task.id)
            .replace("{{task_name}}", task.name)
            .replace("{{status}}", status)
            .replace("{{chat_id}}", named)
            .replace("{{agent}}", task.agent))
        if verdict:
            result_prompt += (f"\n\n[Check {verdict['check']} did not pass after "
                              f"{verdict['round']} round(s): {verdict.get('summary') or ''}]")
        # Appended here, not a template token: task rows stored before this
        # carry the old template and get the note all the same.
        if ending is not None:
            result_prompt += f"\n\n[{ending.callback_note(named)}]"
        if files_note:
            result_prompt += f"\n\n{files_note}"
        if background_pending:
            cont = (f'delegate(continue_id="{named}")' if named
                    else "delegate(continue_id=…)")
            result_prompt += (f"\n\n[The worker's run ended with {background_pending} "
                              f"background job(s) still running; their results are not in "
                              f"this report. Continue the worker with {cont} to collect them.]")
        if missing:
            # First, before the template: the agent reads the worker's last
            # message as the outcome otherwise.
            result_prompt = (f"[{lanes.missing_report_note(cut, named)}]\n\n"
                             + result_prompt)
        await _do_deliver(
            on_complete_session_id or "", on_complete_agent, result_prompt, task,
            chat_id=on_complete_chat_id, wake=wake,
            worker_chat_id=named,
            output_text=output,
            status=status,
            verdict=verdict,
            ending=ending,
            files=files,
            files_skipped=files_skipped,
            background_pending=background_pending,
            report_missing=missing,
        )

    # The worker chat owes this report until the collection above (taken
    # before the task exists, so no turn starts between them).
    lanes.hold_report(worker_chat_id)
    spawn(_finalize_and_deliver(), name="finalize_and_deliver", owner="delivery")


def _ending_fields(ending) -> dict:
    """The delegate_result event's typed ending fields (none without one)."""
    if ending is None:
        return {}
    return {"reason": ending.reason, "resets_at": ending.resets_at}


def _result_extras(verdict: dict | None, files: list | None,
                   files_skipped: list | None, *, background_pending: int = 0,
                   report_missing: bool = False) -> dict:
    """The delegate_result event's optional keys beyond the typed ending: a
    failed run's check verdict, the worker's attached files (both lists
    ride together once either has an entry), the background work still
    running, and ``report_missing`` for a result that ended without a report;
    each absent otherwise, so an older result keeps its shape. The same keys
    ride the notify payload, from which the dashboard socket's rung rebuilds
    the event (``ws/dashboard_server_events.result_event_from_notify``)."""
    out: dict = {"verdict": verdict} if verdict else {}
    if files or files_skipped:
        out["files"] = list(files or [])
        out["files_skipped"] = list(files_skipped or [])
    if background_pending:
        out["background_pending"] = background_pending
    if report_missing:
        out["report_missing"] = True
    return out


async def _do_deliver(session_id: str, agent: str, result_prompt: str, task: shared.TaskDefinition,
                      *, chat_id: str | None = None, output_text: str = "",
                      status: str = run_status.COMPLETED, verdict: dict | None = None,
                      ending=None, files: list | None = None,
                      files_skipped: list | None = None,
                      background_pending: int = 0,
                      report_missing: bool = False,
                      worker_chat_id: str = "", wake: bool = True) -> None:
    """Deliver a delegate result via the session-delivery ladder (``wake``
    False: the result's event alone, written on the chat, no rung).

    The routing itself (WS notify → pump → persistent → one-shot) lives in
    ``core.session.session_delivery.deliver_prompt``; this wrapper owns the
    DELEGATE semantics — the ``delegate_result`` event payload, the live-state
    badge, the ``task_result_prompt`` notify shape, and the echo persistence.
    The persistent/one-shot rungs are passed by name so they resolve through
    this module at call time (tests monkeypatch them here). ``worker_chat_id``
    rides the event for the card's link to the lane and nothing else.
    """
    try:
        from core.session.session_state import mark_delegate_completed
        from storage.pg import run_db

        # Update live state immediately (for reconnect accuracy)
        if chat_id:
            mark_delegate_completed(chat_id, task.name, task_id=task.id, status=status)

        # Delegate event data shared across all paths. task_id is the stable
        # spawn↔result correlation key (the frontend keys the delegate block by it).
        # status (completed/failed/cancelled) drives the badge icon + lets the
        # frontend skip minting an empty bubble for a no-output terminal.
        # The bubble keeps the TAIL on overflow: output_text is the whole lane
        # narration and the worker's actual deliverable is its final message —
        # a head cut always amputates it (the old silent [:2000] did exactly
        # that). The result PROMPT to the parent agent stays uncapped.
        output_preview = lanes._delegate_output_preview(output_text or "")
        # The frame names the delegating chat, so a socket shows it on that
        # chat alone. The stored row needs no name: it is that chat's row.
        delegate_event = {
            "type": wire.DELEGATE_RESULT,
            **({"chat_id": chat_id} if chat_id else {}),
            "task_id": task.id,
            "task_name": task.name,
            "agent": task.agent,
            "output_text": output_preview,
            "status": status,
            **({"worker_chat_id": worker_chat_id} if worker_chat_id else {}),
            **_result_extras(verdict, files, files_skipped,
                             background_pending=background_pending,
                             report_missing=report_missing),
            **(_ending_fields(ending)),
        }
        delegate_event_data = json.dumps({
            "task_id": task.id,
            "task_name": task.name,
            "agent": task.agent,
            "output_text": output_preview,
            "status": status,
            **({"worker_chat_id": worker_chat_id} if worker_chat_id else {}),
            **_result_extras(verdict, files, files_skipped,
                             background_pending=background_pending,
                             report_missing=report_missing),
            **(_ending_fields(ending)),
        }, default=str)

        if not wake:
            await _persist_refused_result(chat_id, session_id, delegate_event, delegate_event_data)
            logger.info("Task result kept as an event only, waking nobody: chat=%s, task=%s",
                        (chat_id or '-')[:8], task.name)
            return

        from core.events import chat_writer
        persisted: list[str] = []
        written: list[asyncio.Future] = []

        def _persist_delegate_event(target_chat_id: str) -> None:
            # The delegate_result event is the source of truth — it renders the
            # delegate's output and completes the badge regardless of whether
            # the LLM-echo delivery succeeds. Invoked exactly once by the
            # ladder on every non-WS path (the dashboard handler persists it
            # on the WS path after rendering). Written on the chat's lane, off
            # the loop, ahead of the echo turn's rows.
            persisted.append(target_chat_id)
            if target_chat_id:
                written.append(chat_writer.submit(
                    target_chat_id,
                    functools.partial(task_store.add_chat_message, target_chat_id, "event", "",
                                      event_type=wire.DELEGATE_RESULT,
                                      event_data=delegate_event_data),
                    label="delegate_result",
                ))

        # Resolve with the originating session's user + role so a user-pinned
        # remote target (or admin-default remote) is honored on the
        # persistent/one-shot rungs — a bare resolve would default to local and
        # miss a user override / break remote start.
        # The chat decides who wakes: a person's own chat as its owner, a
        # Shared-only chat as the person whose session delegated the work,
        # any other (a task or phone chat, none at all) by the worker's scope.
        identity = await resolve_delivery_person(
            chat_id, session_id, task.created_by, task.scope, agent)
        shared_person = identity.person if identity.shared else ""
        if identity.refused:
            # The person no longer holds the agent: the result is kept on
            # the chat and nothing is warmed for them (no rung, no stored
            # wake to replay later).
            await _persist_refused_result(
                chat_id, session_id, delegate_event, delegate_event_data)
            logger.info(
                "Task result kept as an event only: %s no longer "
                "holds agent '%s': chat=%s, task=%s",
                identity.person[:8], agent, (chat_id or '-')[:8], task.name
            )
            return
        deliver_user_sub: str | None = identity.person or None
        deliver_role = identity.role

        async def _on_outcome(outcome: "DeliveryOutcome") -> None:
            # Durable replay: every rung failed, so the orchestrator was never
            # woken (the delegate_result event row IS persisted, but no turn
            # ran). Store the wake on the parent chat; the next warmup/turn
            # claims and injects it so the orchestrator continues then (a
            # manual re-warm included). Here, not after the ladder, so the
            # hand-back of a result queued on a terminal that closed first
            # stores it too.
            if outcome.path == "none" and chat_id:
                if deliver_user_sub and _wake_refused(
                        (await run_db(_standing_of, deliver_user_sub, agent))[0],
                        shared=bool(shared_person)):
                    logger.info(
                        "Delegate wake not stored: %s no longer holds "
                        "agent '%s': chat=%s, task=%s",
                        deliver_user_sub[:8], agent, chat_id[:8], task.name
                    )
                    return
                if await _store_undelivered_wakes(chat_id, [result_prompt],
                                                  person=deliver_user_sub or "",
                                                  role=deliver_role):
                    logger.info(
                        "Delegate wake undeliverable: stored for replay on next "
                        "warmup: chat=%s, task=%s", chat_id[:8], task.name
                    )
            # The delegate_result event is already persisted by the ladder —
            # only the assistant echo (the LLM's reaction to the result) needs
            # a real response. A failed --resume (dead session) returns None
            # and must NOT be saved as the assistant message (the old "No
            # conversation found" bug, now also guarded by a resumability
            # pre-check in _deliver_via_oneshot); a pump-driven echo turn
            # returns "" — the pump persisted the turn itself, saving here
            # would duplicate it. Runs via the ladder's on_outcome hook so a
            # PTY close-handback's late persistent/one-shot echo is saved
            # identically.
            if not outcome.response:
                return
            if outcome.chat_id:
                # On the chat's lane, behind the event row, off the loop.
                await chat_writer.submit(
                    outcome.chat_id,
                    functools.partial(task_store.add_chat_message, outcome.chat_id,
                                      "assistant", outcome.response),
                    label="delegate_echo",
                )
                logger.info("Task result echo saved to chat DB: chat=%s, task=%s", outcome.chat_id[:8], task.name)
            else:
                from core.session import session_state as _state_mod
                _state_mod._save_pending_result(outcome.session_id, outcome.response)
                logger.info("Task result echo saved as pending: session=%s, task=%s", outcome.session_id[:8], task.name)

        # A person's own chat wakes as the chat, any other as nobody's.
        woken = await run_db(chat_rebuild.wake_build, identity.chat, deliver_user_sub,
                             deliver_role)
        try:
            outcome = await chat_rebuild.rebuild_and_resume(
                chat_id or "", result_prompt,
                source="delegate_result",
                session_id=session_id,
                agent=agent,
                person=deliver_user_sub,
                role=deliver_role,
                mode=woken.mode, kind=woken.kind, build=woken.build,
                notify_payload={
                    "type": wire.NOTIFY_TASK_RESULT_PROMPT,
                    # Originating (delegating) chat/session so the dashboard handler
                    # runs the synthesis turn on THIS chat, not whatever the socket
                    # happens to be viewing (chat-scoped server turns). The ladder
                    # overwrites session_id/chat_id with the resolved anchors.
                    "session_id": session_id,
                    "chat_id": chat_id,
                    "task_id": task.id,
                    "task_name": task.name,
                    "result_prompt": result_prompt,
                    "delegate_agent": task.agent,
                    "output_text": output_preview,
                    "status": status,
                    **({"worker_chat_id": worker_chat_id} if worker_chat_id else {}),
                    **_result_extras(verdict, files, files_skipped,
                                     background_pending=background_pending,
                                     report_missing=report_missing),
                    **_ending_fields(ending),
                },
                pump_event=delegate_event,
                persist_event=_persist_delegate_event,
                on_outcome=_on_outcome,
            )
        except Exception:
            # A rung that raises (a broken spawn, a config or DB error) never
            # reaches the ladder's hook: the event row is written if no rung
            # wrote it, and the wake is stored the way the hook stores an
            # undelivered one (the standing re-check included).
            logger.exception(
                "Task result delivery raised: session=%s, task=%s",
                session_id[:8], task.id)
            from core.session.session_delivery import DeliveryOutcome
            if not persisted:
                _persist_delegate_event(chat_id or "")
            outcome = DeliveryOutcome("none", chat_id=chat_id or "", session_id=session_id)
            await _on_outcome(outcome)
        for row in written:
            # The event row is on the chat before the badge frame goes out
            # (the lane logs a failed write itself).
            with contextlib.suppress(Exception):
                await row
        logger.info(
            "Task result delivered via %s: "
            "session=%s, task=%s",
            outcome.path, outcome.session_id[:8] if outcome.session_id else '-', task.name
        )

        # Live badge correctness: the ws/steer/pump rungs deliver the
        # delegate_result frame to the viewer themselves; the other rungs
        # (pty/persistent/oneshot/none) carry no socket, so a live viewer's
        # "Delegated" badge would spin until a full history reload. Broadcast
        # the terminal frame chat-scoped, independent of the wake delivery.
        # Idempotent on the frontend (status keyed by task_id).
        if chat_id and outcome.path not in ("ws", "steer", "pump"):
            from core.session.session_state import broadcast_chat_frame
            broadcast_chat_frame(chat_id, delegate_event)
    except Exception as e:
        logger.exception("Task result delivery failed: session=%s, task=%s: %s", session_id[:8], task.id, e)


async def _persist_refused_result(chat_id: str | None, session_id: str,
                                  delegate_event: dict, delegate_event_data: str) -> None:
    """The delegate_result event of a delivery that warms nothing (a person
    who left the agent, a result that wakes nobody): written on the chat's
    lane (the chat resolved by its session when the caller had none) and
    shown to a live viewer."""
    from core.events import chat_writer
    from core.session.session_state import broadcast_chat_frame
    from storage.pg import run_db
    target = chat_id or ""
    if not target and session_id:
        row = await run_db(task_store.get_chat_by_session, session_id)
        target = (row or {}).get("id") or ""
    if not target:
        return
    await chat_writer.submit(
        target,
        functools.partial(task_store.add_chat_message, target, "event", "",
                          event_type=wire.DELEGATE_RESULT, event_data=delegate_event_data),
        label="delegate_result",
    )
    broadcast_chat_frame(target, delegate_event)


async def redeliver_pending_wakes(machine_id: str | None = None,
                                  session_ids: list[str] | tuple[str, ...] = ()) -> int:
    """Redeliver delegate wakes parked on chats (stored when every delivery
    rung failed — typically the proxy restarted inside the delivery window).

    Startup runs it once after boot settles; every satellite reconnect runs it
    scoped to that machine's chats, pinned there or running one of
    ``session_ids`` (an idle satellite chat's wake can only deliver once its
    machine is back). Per-chat claims are atomic, so a
    concurrent warmup/turn claim wins cleanly and this pass no-ops for that
    chat. A chat whose session is parked for Mode C re-adopt is skipped —
    the reconnect pass retries it after the adopt settles. Failed deliveries
    re-persist the original wakes (same caller-side contract as
    ``_deliver_task_result``). Chats are delivered ``_SWEEP_CONCURRENCY`` at a
    time. Returns the number of chats woken."""
    rows = await asyncio.to_thread(
        task_store.list_chats_with_pending_wakes, machine_id, session_ids,
    )
    gate = asyncio.Semaphore(_SWEEP_CONCURRENCY)

    async def _one(chat: dict) -> bool:
        async with gate:
            return await _redeliver_chat(chat)

    results = await asyncio.gather(*(_one(chat) for chat in rows), return_exceptions=True)
    woken = 0
    for chat, result in zip(rows, results, strict=False):
        if isinstance(result, BaseException):
            logger.error("Wake redelivery: chat %s failed", chat["id"][:8], exc_info=result)
        elif result:
            woken += 1
    return woken


async def _redeliver_chat(chat: dict) -> bool:
    """One chat of the wake sweep; True when a rung woke it. The claimed
    wakes go in groups, one per person and role they were stored for, one
    group after another (a person's own chat: all as its owner). A note
    (``WAKE_NOTE``) stays stored for the chat's next typed message."""
    from services.scheduler import run_recovery
    from storage.pg import run_db

    chat_id = chat["id"]
    if (chat.get("session_id") or "") in run_recovery._parked:
        return False
    # A chat whose cut turn the continue pass has not settled is the pass's:
    # a wake that started a session there first would take the turn's place.
    from storage.chat import cut_records
    if await run_db(cut_records.unsettled_of, chat_id) is not None:
        return False
    agent = chat.get("agent") or ""
    owner = chat.get("user_sub") or ""
    personal = bool(owner) and not is_synthetic_owner(owner)
    if personal:
        # Checked before the claim, so a failed read leaves the wakes stored.
        standing, _row = await run_db(_standing_of, owner, agent)
        if standing == roles.NO_ACCESS:
            dropped = await run_db(functools.partial(
                task_store.claim_pending_delegate_wake, chat_id, notes=True))
            logger.info(
                "Wake redelivery: chat %s dropped %d wake(s): its owner no longer "
                "holds agent '%s'", chat_id[:8], len(dropped), agent,
            )
            return False
    records = await run_db(task_store.claim_pending_wake_records, chat_id)
    groups: dict[tuple[str, str, str], list[str]] = {}
    for rec in records:
        key = (owner, "", "") if personal else (rec["person"], rec["role"], rec["by"])
        groups.setdefault(key, []).append(rec["prompt"])
    woken = False
    for (person, role, by), prompts in groups.items():
        try:
            woken = await _redeliver_group(chat, prompts, person=person, role=role,
                                           by=by) or woken
        except Exception:
            # One group's broken spawn (bad config, dead layer) must neither
            # kill the sweep nor eat the CLAIMED wakes: re-persist and move
            # on (a corrupt user config.toml fails the oneshot, live-hit).
            logger.exception(
                "Wake redelivery: chat %s delivery raised, re-storing %d wake(s)",
                chat_id[:8], len(prompts),
            )
            for w in prompts:
                await run_db(functools.partial(
                    task_store.append_pending_delegate_wake, chat_id, w,
                    person=person, role=role, by=by))
    return woken


def _sweep_identity(chat: dict, person: str, role: str, by: str,
                    ) -> tuple[str | None, str, bool, bool]:
    """Who one group of stored wakes runs as: ``(user_sub, role, shared,
    refused)``. A person's own chat runs as its owner at their row; a stored
    person as that person (on a Shared-only chat at their standing, refused
    below the editor tier); no person at the role the wake was stored with,
    read again from its creator's row when a creator is on record, else the
    owner kind's default (a task chat or none: manager; a shared or phone
    chat: viewer). A lane whose person cannot run it any more is refused:
    no start is ever made for it, so its wakes are dropped. Synchronous:
    call it on the DB executor."""
    from core.config import spender
    paid = spender.round_spender(chat.get("id") or "")
    if paid is not None and paid.refused:
        return None, role or roles.VIEWER, False, True
    agent = chat.get("agent") or ""
    owner = chat.get("user_sub") or ""
    # Off a Shared-only chat the role the dashboard reads for the person
    # (``acting_role_of``: a platform admin is admin on every agent), the
    # one the wake's own build reads again (``_role_after_wait``).
    from auth.providers import acting_role_of
    if owner and not is_synthetic_owner(owner):
        standing, _row = _standing_of(owner, agent)
        return owner, acting_role_of(owner, agent), False, standing == roles.NO_ACCESS
    if person:
        shared = bool(_shared_chat_person(chat, person))
        standing, _row = _standing_of(person, agent)
        return (person, standing if shared else acting_role_of(person, agent), shared,
                _wake_refused(standing, shared=shared))
    if by and by != agent:
        return None, roles.row_role(task_store.get_user_agent_roles(by), agent) or roles.VIEWER, \
            False, False
    if role:
        return None, role, False, False
    if not owner or is_task_chat_owner(owner):
        return None, roles.MANAGER, False, False
    return None, roles.VIEWER, False, False


async def _redeliver_group(chat: dict, prompts: list[str], *, person: str, role: str,
                           by: str) -> bool:
    """Deliver one group of claimed wakes; True when a rung woke the chat.
    A wake that runs as nobody below the editor tier can start no session
    (it would run from the agent's own state, which the start floor
    refuses), so it rides a live process only: undelivered, it stays for a
    person's next turn on a shared chat and is dropped elsewhere."""
    from storage.pg import run_db

    chat_id = chat["id"]
    agent = chat.get("agent") or ""
    owner = chat.get("user_sub") or ""
    user_sub, deliver_role, shared, refused = await run_db(
        _sweep_identity, chat, person, role, by)
    if refused:
        logger.info(
            "Wake redelivery: chat %s dropped %d wake(s): %s no longer holds agent '%s'%s",
            chat_id[:8], len(prompts), (user_sub or "-")[:8], agent,
            " at the editor tier" if shared else "",
        )
        return False
    spawns = user_sub is not None or roles.can_edit(deliver_role)
    # Kept for a person's next turn only on a shared chat of an agent that is
    # Shared-only now: the chat of an agent that left the mode takes no such
    # turn, so the wake is dropped there as the continuation fire drops it.
    keep = spawns
    if not keep and is_shared_chat_owner(owner):
        from core.session.visibility import is_shared_only
        keep = bool(await run_db(is_shared_only, agent))

    async def _on_outcome(outcome: "DeliveryOutcome") -> None:
        # Same contract as _do_deliver's hook: every rung failed →
        # re-store the original wakes; pump-driven turns return ""
        # (already persisted); dead resumes return None.
        if outcome.path == "none":
            if user_sub and _wake_refused(
                    (await run_db(_standing_of, user_sub, agent))[0], shared=shared):
                return
            if not keep:
                logger.info(
                    "Wake redelivery: chat %s dropped %d wake(s): no live process took "
                    "them and none may start below the editor tier", chat_id[:8], len(prompts),
                )
                return
            await _store_undelivered_wakes(chat_id, prompts, person=person, role=role, by=by)
        if outcome.response and outcome.chat_id:
            from core.events import chat_writer
            await chat_writer.submit(
                outcome.chat_id,
                functools.partial(task_store.add_chat_message, outcome.chat_id,
                                  "assistant", outcome.response),
                label="delegate_echo",
            )

    woken = await run_db(chat_rebuild.wake_build, chat, user_sub, deliver_role)
    outcome = await chat_rebuild.rebuild_and_resume(
        chat_id, "\n\n".join(prompts),
        source="delegate_result_replay",
        session_id=chat.get("session_id") or "",
        agent=agent,
        person=user_sub,
        role=deliver_role,
        mode=woken.mode, kind=woken.kind, build=woken.build,
        spawns=spawns,
        on_outcome=_on_outcome,
    )
    if outcome.path == "none":
        logger.info(
            "Wake redelivery: chat %s undeliverable (%d wake(s))", chat_id[:8], len(prompts),
        )
        return False
    logger.info(
        "Wake redelivery: chat %s woken via %s (%d wake(s))",
        chat_id[:8], outcome.path, len(prompts),
    )
    return True


async def _deliver_via_persistent(
    session_id: str, agent: str, result_prompt: str,
    *, user_sub: str | None = None, role: str, chat_id: str = "", driven_by: str = "",
) -> str | None:
    """Path A/B: Use alive persistent session. ``driven_by``: what started
    the turn (``rebuild_and_resume``'s source), for the pump that runs it.

    With a delegating chat the echo turn runs through a headless pump (see
    ``_run_echo_turn_pumped``) and returns "" — the pump persisted it.
    Chat-less deliveries keep the direct collection so the caller can save the
    response as a pending result. None = this rung could not deliver."""
    from core.events.common_events import TEXT
    from core.session import session_state as _state
    from storage.pg import run_db
    if drain.holding():
        # A planned stop holds new turns: the caller stores the wake, and
        # the boot (or the drain's resume) delivers it.
        return None
    chat_row = await run_db(task_store.get_chat, chat_id) if chat_id else None
    if chat_row and user_sub:
        # The placement lock: a live session on another person's computer
        # is never driven for this person. The next rung's spawn meets the
        # guard and the wake is kept for the chat's next turn.
        from services.remote import chat_move
        if await run_db(chat_move.lock_sentence, chat_row, user_sub):
            return None
    pin = await run_db(_chat_pin, chat_row if user_sub else None, user_sub)
    # The CHAT's engine, as the next rung reads it: asked of the agent's
    # default, a chat on its other engine has no live session to find.
    try:
        from core.session.session_manager import layer_for_chat
        layer = layer_for_chat(agent, {**(chat_row or {}), "execution_target": pin},
                               user_sub=user_sub, role=role)
    except RuntimeError:
        # Resolved remote target offline / disabled — session unreachable here.
        return None
    if not await layer.is_session_alive(session_id):
        return None
    try:
        # Update last_active so this session stays "most recent" for the agent.
        # Without this, task sessions could have a more recent timestamp and
        # /v1/session/current would return the wrong session during re-delegation.
        if session_id in _state._sessions:
            _state._sessions[session_id]["last_active"] = datetime.now(timezone.utc).isoformat()
            _state._save_sessions()
        if chat_id:
            return await _run_echo_turn_pumped(
                layer, session_id, chat_id, agent, result_prompt, person=user_sub or "",
                source=driven_by,
            )
        parts: list[str] = []
        async with layer.session_lock(session_id):
            async for event in layer.send_message(session_id, result_prompt):
                if event.type == TEXT:
                    content = event.data.get("content", "")
                    if content:
                        parts.append(content)
        return "".join(parts) if parts else ""
    except asyncio.TimeoutError:
        # Lock held too long — session may be stuck but don't close it
        return None
    except Exception:  # noqa: BLE001  # a failed persistent delivery falls through to the next rung
        # Session broken (BrokenPipeError, OSError, RuntimeError, etc.)
        await layer.close_session(session_id)
        return None


async def _deliver_via_oneshot(
    session_id: str, agent: str, result_prompt: str,
    *, user_sub: str | None = None, role: str, chat_id: str = "",
    permission_mode: str, client_type: str, build: str,
    fresh_without_session: bool = False, driven_by: str = "",
) -> str | None:
    """Path C: Dead session, use one-shot --resume via execution layer.

    With a delegating chat the resumed turn runs through a headless pump (see
    ``_run_echo_turn_pumped``) and returns "" — the pump persisted it.
    Chat-less deliveries keep the direct collection so the caller can save the
    response as a pending result. The spawn takes a slot of the admission
    budget first (``concurrency.reserve_background``); None when the wake
    waited out its turn or gave way to a person on the chat.

    The mode, the kind and the builder of the session it starts are the
    caller's words (``core/session/chat_rebuild.py``), with no default: a
    wake names the unattended mode and no client kind, the platform's resume
    after the agent's own move the chat's own mode and kind
    (``permission_mode``, ``client_type``: the hook judges the turn as the
    chat's), and ``build`` the builder (``chat_rebuild.AS_WAKE``, or
    ``AS_CHAT`` for what the person's own warm-up builds, at the role the
    dashboard reads for them). ``fresh_without_session`` opts in to a fresh
    start for a chat with no session at all (a summarised move, nothing to
    probe). A
    terminal a wake re-warms notes the prompt it types, so the transcript
    tailer writes no person's row for it (the row is the ladder's)."""
    from core.session import session_state as _state
    from core.session.session_manager import get_execution_layer, resolve_execution_path
    from services.remote import remote_status
    from storage.pg import run_db

    if drain.holding():
        return None
    # Update last_active upfront so concurrent lookups (e.g. delegate_task
    # calling /v1/session/current during the resumed session) see this session
    # as most recent before the layer sends the message.
    if session_id in _state._sessions:
        _state._sessions[session_id]["last_active"] = datetime.now(timezone.utc).isoformat()
        _state._save_sessions()
    # Resolve the target once so the layer and the config agree — a bare
    # resolve would default the config target to "local" and the remote layer
    # would reject the start (RemoteExecutionLayer called with local target).
    # The CHAT's pinned layer beats the agent default: a wake for a chat on
    # the non-default layer (agent default codex, chat on claude) resolved
    # the wrong layer and always failed the resumability pre-check.
    _chat_row: dict | None = None
    chat_exec = ""
    if chat_id:
        _chat_row = await asyncio.to_thread(task_store.get_chat, chat_id)
        chat_exec = (_chat_row or {}).get("execution_path") or ""
    exec_path = resolve_execution_path(agent, chat_exec)
    pin = await run_db(functools.partial(_chat_pin, _chat_row, user_sub,
                                         as_chat=build == chat_rebuild.AS_CHAT))
    target = pin or (await run_db(
        remote_status.resolve_execution_target, agent, user_sub, role))[0]
    if placement.is_offline_sentinel(target):
        logger.warning(
            "Delegate one-shot delivery skipped — agent '%s' remote target "
            "offline: session=%s", agent, session_id[:8]
        )
        return None
    # The compute machine serving the platform's place, read once for this
    # delivery: the slot, the layer and the session's config agree on it
    # (the builders take it as their ``prefer``). With none the wake is
    # kept for later, as for a machine that is offline.
    from services.remote import remote_status
    place = remote_status.chat_place(_chat_row)
    try:
        compute_row = await asyncio.to_thread(functools.partial(
            remote_status.compute_row_for, target, exec_path,
            prefer=place.machine, shared_folder=place.shared, agent=place.agent_for(agent)))
    except placement.NoComputeMachine:
        logger.warning(
            "Delegate one-shot delivery skipped: no compute machine serves "
            "agent '%s' now: session=%s", agent, session_id[:8]
        )
        return None
    built = placement.from_machine(placement.KIND_COMPUTE, compute_row) if compute_row else None
    layer = get_execution_layer(
        agent, execution_path=exec_path, user_sub=user_sub, role=role,
        execution_target=target, **({"placement_built": built} if built else {}),
    )
    # Resumability pre-check: if the session has no conversation file to
    # --resume, the oneshot would emit a "No conversation found" error that we'd
    # otherwise collect and save as the delegate's response. Skip instead — the
    # delegate_result event (saved by _do_deliver) already carries the output.
    username = (await run_db(task_store.get_username_by_sub, user_sub)) if user_sub else ""
    # The conversation lives in the session's MOUNT: a Shared-only chat's in
    # the agent scope even when a person is on the delivery.
    from core.session.visibility import SCOPE_USER
    vis = await run_db(_wake_visibility, agent, username or "", role, user_sub)
    fresh_from, fresh = "", False
    from core.session.session_manager import capabilities_for_path
    verdict = ResumeVerdict.RESUMABLE
    if fresh_without_session and chat_id and wake_fresh.applies_without_session(
            session_id, _chat_row):
        # No session to ask about anywhere (a summarised move): a fresh one
        # starts under a new id, the chat's pending seed riding ahead.
        fresh, session_id = True, str(uuid.uuid4())
    elif not capabilities_for_path(exec_path).behaviour.rebuilds_history_from_db:
        # (an engine that rebuilds its history from the chat's rows resumes
        # under the same id with no file to ask about)
        verdict = await settle_resume(
            layer, session_id, agent_name=agent, username=vis.mount_username,
            budget_s=RESUME_PROBE_BUDGET_BACKGROUND_S,
        )
    if verdict is ResumeVerdict.UNKNOWN:
        # The machine did not answer within the budget: this rung could not
        # deliver, and the caller stores the wake for the chat's next turn.
        logger.info(
            "Delegate one-shot delivery deferred — the machine of session %s did "
            "not answer the resume probe (agent=%s)", session_id[:8], agent
        )
        return None
    if verdict is ResumeVerdict.FRESH:
        if not (chat_id and wake_fresh.applies(session_id, target, _chat_row)):
            logger.info(
                "Delegate one-shot delivery skipped — session %s has no "
                "resumable conversation (agent=%s)", session_id[:8], agent
            )
            return None
        fresh_from, session_id, fresh = session_id, str(uuid.uuid4()), True
    # Reserve before anything is built for the spawn: on a full box the wake
    # waits here like a task (never taking the last slot a person needs) and
    # gives way to a person who opens the chat meanwhile. A wake that gives
    # up returns None: the caller stores it for the chat's next turn.
    from core import concurrency
    state = await concurrency.reserve_background(
        session_id, target=placement.admission_target(target, built), execution_path=exec_path,
        timeout_s=config.ADMISSION_WAKE_WAIT_S,
        superseded=lambda: _wake_superseded(session_id, chat_id),
    )
    if state in ("superseded", "timeout"):
        logger.info(
            "Delegate one-shot delivery deferred (%s): session=%s, "
            "chat=%s",
            state, session_id[:8], chat_id[:8] if chat_id else '-'
        )
        return None
    reserved = state == "reserved"
    try:
        spawn_role = (await _role_after_wait(agent, user_sub, role, target, session_id,
                                             agent_state=SCOPE_USER not in vis.available_scopes,
                                             pinned=bool(pin),
                                             as_chat=build == chat_rebuild.AS_CHAT)
                      if user_sub else role)
        if spawn_role is not None and build == chat_rebuild.AS_CHAT and driven_by in chat_rebuild.TYPED:
            # A key's message runs in the chat's mode as it is now, read
            # again after the slot's wait as the role is: a mode changed
            # meanwhile starts nothing, and the request is sent again.
            fresh_row = await run_db(task_store.get_chat, chat_id) if chat_id else None
            if not fresh_row or await run_db(chat_rebuild.chat_mode, fresh_row) != permission_mode:
                spawn_role = None
            else:
                _chat_row = fresh_row
        started, response = False, None
        if spawn_role is not None:
            started, response = await _spawn_wake_session(
                layer, session_id, agent, result_prompt, user_sub=user_sub, role=spawn_role,
                chat_id=chat_id, chat_row=_chat_row, exec_path=exec_path, target=target,
                username=username or "", fresh_from=fresh_from, fresh=fresh,
                permission_mode=permission_mode, client_type=client_type, build=build,
                driven_by=driven_by,
                **({"compute": compute_row} if compute_row else {}),
            )
    except BaseException:
        if reserved:
            _release_wake_slot(session_id, "" if fresh else chat_id)
        raise
    if not started and reserved:
        # A fresh id is this wake's alone: nobody else can have adopted it.
        _release_wake_slot(session_id, "" if fresh else chat_id)
    return response
