"""The task runner: ``_fire_task`` (the APScheduler job entry), ``_execute_task``
and ``_run_task`` (mutually recursive through the retry; the frame around
``_run_task_body``, the admitted run), the run's chat row, the run's end
(``_end_run``: the terminal stamp, then the stream's ``done``) and the
run-event broadcast.

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
from typing import TYPE_CHECKING, Protocol, TypeGuard, cast

import psycopg

import config
from core import drain, placement
from core.background import spawn
from core.permission_modes import task_run_mode
from core.session import session_kind
from core.session.visibility import shared_chat_owner, task_chat_owner
from services.scheduler import (
    firing,
    lanes,
    shared,
    task_kinds,
    task_run,
)
from storage import database as task_store
from storage.agents import agent_store
from storage.agents.schema import DEFAULT_EXECUTION_PATH
from storage.automation import run_status

if TYPE_CHECKING:
    from core.config.spender import Spender

logger = logging.getLogger("otodock.scheduler")


def _rebuilds_from_rows(agent_cfg) -> bool:
    """The round's engine keeps no conversation of its own (Direct LLM): it
    rebuilds it from the chat rows at every start."""
    from core.session.session_manager import capabilities_for_path
    return bool(capabilities_for_path(agent_cfg.execution_path or "")
                .behaviour.rebuilds_history_from_db)


async def _fire_task(task: shared.TaskDefinition) -> None:
    """APScheduler calls this as an async job. Creates a fire-and-forget task.

    The job holds the definition it was registered with, and an edit that
    leaves the timing alone does not re-register it: the row is read again
    so the edited prompt, model or checks are what runs. A row that is gone
    (deleted in plain SQL, as a removal cascade does) or paused fires
    nothing: the job goes with it. Only a read that FAILED fires the
    registered definition."""
    from storage.pg import run_db
    shared._fires_under_way.add(task.id)
    handed = False
    try:
        try:
            row = await run_db(task_store.get_dynamic_task, task.id)
        except psycopg.Error:
            logger.warning("Task %s: the row could not be re-read; firing as registered",
                           task.id,
                           exc_info=True)
            row = None
        else:
            if not row or not row.get("enabled", True):
                logger.info("Task %s: the row is %s; "
                            "the job is removed and nothing fires",
                            task.id, 'gone' if not row else 'paused')
                _drop_job(task.id)
                return
            from services.scheduler.scheduler import _row_to_task
            task = _row_to_task(row)
        spawn(_execute_fired(task), name="execute_task", owner="runner")
        handed = True
    finally:
        if not handed:
            shared._fires_under_way.discard(task.id)


async def _execute_fired(task: shared.TaskDefinition) -> str:
    """A scheduled fire's execution, under way until it returns."""
    try:
        return await _execute_task(task, trigger_type=task_kinds.TRIGGER_SCHEDULED)
    finally:
        shared._fires_under_way.discard(task.id)


def _drop_job(task_id: str) -> None:
    from services.scheduler import scheduler as _api
    with contextlib.suppress(Exception):
        _api._scheduler.remove_job(f"task_{task_id}")
    shared._dynamic_task_ids.discard(task_id)


def _is_valid_session_uuid(v) -> TypeGuard[str]:
    """True iff ``v`` is a valid UUID string usable for ``--session-id`` /
    ``--resume``. Guards against legacy rows or buggy callers writing
    bool-as-string ("false", "true") or empty strings into
    ``dynamic_tasks.continue_session``.
    """
    if not v or not isinstance(v, str):
        return False
    try:
        uuid.UUID(v)
        return True
    except (ValueError, AttributeError):
        return False


def _create_task_chat_row(chat_id: str, run_id: str, task: shared.TaskDefinition) -> None:
    """The run's chat row + ``runs.chat_id`` stamp (sync — call via to_thread).

    Task-surface delegate workers keep the shared ``agent::`` owner for agent
    scope, the delegate name as seed title, the delegated origin (purple
    worker accent + LLM title upgrade) and the delegation lineage for the
    dock's lane graph; plain runs use the ``task::`` owner convention. Both
    carry ``source_type='task'`` — the row's kind (``session_kind.of_chat``);
    rows minted before that write carry ``chat`` with the same ``task-`` id.

    The row is stamped with the engine and model this run will ACTUALLY
    use (``_task_engine_and_model``: the pins, else the creator's own engine
    for a user-scope run, else the agent's default), resolved ONCE here:
    the builder reads the row back, so the row, the session and billing
    (``services/execution/stream_pump.py`` → ``usage_records``, which reads
    the model off the chat row) never name different engines, and a re-warm
    of the run's chat follows the engine the run used."""
    task_path, task_model = _task_engine_and_model(task)
    kind = task_kinds.of(task)
    if kind.worker:
        worker_sub = (cast("str", task.created_by) if task.scope == "user"
                      else shared_chat_owner(task.agent))
        row = task_store.create_chat(chat_id, worker_sub, task.agent, task_run_mode(task),
                                     model=task_model, execution_path=task_path,
                                     source_type=session_kind.TASK.source_type,
                                     origin="delegated",
                                     parent_chat_id=task.parent_chat_id or "",
                                     project_id=task.project_id or "",
                                     delegate_role="worker",
                                     title=task.name)
    else:
        user_sub = (cast("str", task.created_by) if task.scope == "user"
                    else task_chat_owner(task.agent))
        row = task_store.create_chat(chat_id, user_sub, task.agent, task_run_mode(task),
                                     model=task_model, execution_path=task_path,
                                     source_type=session_kind.TASK.source_type,
                                     # A check's judge run (CHECKS.md) says what it
                                     # is in the task history; other runs are
                                     # titled from their first prompt.
                                     title=task.name if kind.judge else "",
                                     checks=json.dumps(list(task.checks)) if task.checks else "")
    task_store.update_run(run_id, chat_id=chat_id)
    try:
        from services.apps import catalog
        catalog.announce_new_chat(row)
    except Exception:  # noqa: BLE001  # the catalog delta is best effort, the broadcast already went out
        pass


class _EngineInputs(Protocol):
    """What ``_task_engine_and_model`` reads of a run: a task definition,
    or the delegation spawn's view of its worker (``api/tasks/delegation``),
    which hands its spender instead of a task type and a record."""
    agent: str
    scope: str
    created_by: str | None
    override_execution_path: str | None
    override_model: str | None


def _task_engine(task: _EngineInputs, *, payer: "Spender | None" = None) -> tuple[str, str]:
    """The engine and model this run spawns on. An engine pin as given. A
    model pin without one runs where the model is offered
    (``engine_resolution.engine_for_model``, the rule
    ``spawn_authz.validate_spawn_overrides`` judged it by): the engine the
    roster marked default for the person first, else the agent's default
    engine, else the first allowed engine that offers it. Without a pin, a
    run that spends a person's accounts (``core/config/spender.py``; the
    spawn hands its own ``payer``) takes that person's own engine among the
    agent's (``engine_resolution``, the rule their new chat follows). Else
    the agent's default engine. The model is the person's resolution's,
    else ``""`` (``_task_engine_and_model`` finishes it)."""
    from core.config import spender
    from services.engines import engine_resolution
    agent = agent_store.get_agent(task.agent) or {}
    path = task.override_execution_path or ""
    model = ""
    person = (payer if payer is not None else spender.run_spender(task)).sub or ""
    pinned = config.live_model_id(task.override_model or "")
    if not path and person:
        resolved = engine_resolution.resolve_session_engine(task.agent, person)
        path, model = resolved.execution_path, resolved.model
    if not path:
        path = agent.get("execution_path") or DEFAULT_EXECUTION_PATH
    if pinned and not task.override_execution_path:
        try:
            path = engine_resolution.engine_for_model(agent, pinned, first=path) or path
        except psycopg.Error:
            logger.warning("Task %s: the engine of the pinned model %s could not be read; "
                           "running on %s", getattr(task, "id", "?"), pinned, path)
        model = ""
    return path, model


def _task_engine_and_model(task: _EngineInputs, *,
                           payer: "Spender | None" = None) -> tuple[str, str]:
    """``_task_engine``'s engine with the model this run takes: the pin,
    else the person's, else the engine's for this agent
    (``config.resolve_run_model``, layer-aware, which raises when the
    engine has no enabled model)."""
    path, model = _task_engine(task, payer=payer)
    pinned = config.live_model_id(task.override_model or "")
    if pinned:
        model = pinned
    elif not model:
        model = config.resolve_run_model(task.agent, None, layer=path)
    return path, model


def _task_layer(task: shared.TaskDefinition) -> str:
    """The engine this run will use (``_task_engine``): the limit checks
    read the engine alone, so an engine with no enabled model still names
    itself here and fails at the spawn, as it always did."""
    return _task_engine(task)[0]


async def _pool_cap_block(task: shared.TaskDefinition) -> str:
    """The subscription pool cap on this run's engine and payer
    (``services.billing.pool_caps``): the reason when it blocks, else "".
    ``continue`` lets the run proceed when a key is there to continue on
    (the pool applies the exclusion at spawn)."""
    from core.config import spender
    from services.billing import pool_caps
    from services.engines import subscription_pool
    # The pool the spawn draws on (``core/config/spender.py``), not the
    # scope the row stores: a person's cap for a run their accounts pay,
    # the platform's for the shared subscriptions.
    payer = await asyncio.to_thread(spender.run_spender, task)
    scope, target = ("user", payer.sub) if payer.sub else ("platform", "")
    layer = await asyncio.to_thread(_task_layer, task)
    cap = await asyncio.to_thread(pool_caps.evaluate, scope, target, layer)
    if cap.allowed:
        return ""
    if cap.on_reached == "continue" and await asyncio.to_thread(
        subscription_pool.cap_continue_available, layer, target or None,
    ):
        return ""
    logger.warning("Task %s blocked by the %s pool cap: %s", task.id, scope, cap.hit_text())
    return cap.short_reason()


async def _execute_task(task: shared.TaskDefinition, trigger_type: str = "scheduled",
                        trigger_source: str | None = None,
                        prompt_override: str | None = None, attempt: int = 1,
                        trigger_payload: dict | None = None) -> str:
    from core.session import session_state as _state

    kind = task_kinds.of(task)
    # Continuations are wake deliveries, not LLM task runs — no run row, no
    # slot, no usage pre-check (the driven turn bills like any chat turn).
    if kind.fires == task_kinds.WAKE:
        if drain.holding():
            # Held for a planned stop: its row stays unfired, and the boot
            # (or the drain's resume) fires it (``scheduler.fire_held``).
            logger.info("Continuation %s held: the platform is holding new work", task.id)
            shared._held_wakes.append(task.id)
            return ""
        return await firing._fire_continuation(task)

    # An app handler's schedule (APPS.md "Handlers"): a delivery row and a
    # wake of the app's server, never a session — before any identity work.
    if kind.fires == task_kinds.HANDLER:
        from services.apps import app_handlers
        return await app_handlers.fire_scheduled(task, trigger_type=trigger_type)

    from storage.pg import run_db

    run_id = f"run-{uuid.uuid4().hex[:12]}"
    final_prompt = prompt_override or task.prompt
    if task.extra_context:
        final_prompt += "\n\n" + "\n\n".join(task.extra_context)

    # ── Usage limit check before execution ──
    try:
        from services.billing import usage_service
        blocked_by = ""
        # The limit of whoever pays (``core/config/spender.py``): the
        # person's for a run their accounts pay, the agent's otherwise.
        from core.config import spender
        payer = await run_db(spender.run_spender, task)
        if payer.sub:
            payer_row = await run_db(task_store.get_user, payer.sub)
            limit_status = await asyncio.to_thread(
                usage_service.check_user_limit, payer.sub, (payer_row or {}).get("role", "member"))
            if not limit_status["allowed"]:
                logger.warning("Task %s blocked by user limit: %s", task.id, payer.sub)
                blocked_by = "User usage limit exceeded"
        else:
            limit_status = await asyncio.to_thread(
                usage_service.check_agent_limit, task.agent
            )
            if not limit_status["allowed"]:
                logger.warning("Task %s blocked by agent limit: %s", task.id, task.agent)
                blocked_by = "Agent usage limit exceeded"
        if not blocked_by:
            blocked_by = await _pool_cap_block(task)
        if blocked_by:
            task_type = task_kinds.run_kind_of(task)
            await run_db(
                task_store.create_run, run_id, task.id, task.agent,
                trigger_type, trigger_source, final_prompt, task_type,
                task.scope, _run_creator(task), task.spender_sub,
            )
            await run_db(
                task_store.update_run, run_id,
                status=run_status.LIMIT_EXCEEDED,
                error_message=blocked_by,
                completed_at=datetime.now(timezone.utc).isoformat(),
            )
            return run_id
    except Exception as e:  # noqa: BLE001  # a failed limit check lets the task run, as it always did
        logger.error("Usage limit check failed for task %s: %s", task.id, e)

    # Scheduled fires only (manual Run-Now has a human waiting and event
    # triggers should react immediately): the spacing slot, taken only by a
    # fire that passed the limits, at most one waiting fire per task, and
    # the row read again after the wait (a delete, a pause, an edit or a
    # transfer during the wait applies). A retry continues its own run.
    if attempt == 1 and task_kinds.spaced(trigger_type):
        if task.id in shared._active_task_ids:
            logger.warning(
                "Skipping duplicate concurrent run for task %s: "
                "run %s still active",
                task.id, shared._active_task_ids[task.id]
            )
            return shared._active_task_ids[task.id]
        if task.id in firing._spacing_pending:
            logger.info("Task %s: a fire is already waiting for its spawn slot; "
                        "this one is dropped", task.id)
            return ""
        firing._spacing_pending.add(task.id)
        try:
            await firing._spawn_spacing_gate(task.id)
            row = await run_db(task_store.get_dynamic_task, task.id)
        finally:
            firing._spacing_pending.discard(task.id)
        if not row or not row.get("enabled", True):
            logger.info("Task %s: the row was %s "
                        "during the spawn wait; nothing fires",
                        task.id, 'deleted' if not row else 'paused')
            return ""
        from services.scheduler.scheduler import _row_to_task
        task = _row_to_task(row)
        if not prompt_override:
            final_prompt = task.prompt + (
                "\n\n" + "\n\n".join(task.extra_context) if task.extra_context else "")

    task_type = task_kinds.run_kind_of(task)

    # Dedup guard (general, every task type): never run the same task concurrently.
    # Reject a duplicate only on the first attempt; a retry (attempt > 1) is a
    # deliberate continuation so it re-reserves without the reject check. The check
    # + reserve are adjacent (no await between) → race-free on the event loop.
    # Released in _run_task's finally, guarded on run_id so a retry hand-off doesn't
    # release its successor's reservation. Logged, never a silent drop. (The
    # delegate "3 runs" cascade is fixed by reliable terminal delivery — each
    # delegate_task mints a distinct task_id — so this targets scheduled+manual
    # collisions / standalone-retry double-fires.) A Run Now that started
    # during a scheduled fire's spawn wait wins here.
    if attempt == 1 and task.id in shared._active_task_ids:
        logger.warning(
            "Skipping duplicate concurrent run for task %s: "
            "run %s still active",
            task.id, shared._active_task_ids[task.id]
        )
        return shared._active_task_ids[task.id]
    shared._active_task_ids[task.id] = run_id

    # Session ID must be a valid UUID: Claude Code validates this for both
    # ``--session-id`` (new session) and ``--resume`` (existing session).
    # If continue_session was stored as something other than a UUID (e.g. a
    # stale row that wrote "false" as a string), fall back to a fresh UUID
    # so the task still fires.
    if _is_valid_session_uuid(task.continue_session):
        session_id = task.continue_session
    else:
        if task.continue_session:
            logger.warning(
                "Task %s has non-UUID continue_session=%r; generating a fresh "
                "session_id (won't resume prior context)",
                task.id, task.continue_session,
            )
        session_id = str(uuid.uuid4())

    # Mark as task session so /v1/session/current excludes it.
    # Without this, task sessions (which share the agent name and have more recent
    # last_active) would be returned instead of the main interactive session,
    # causing callbacks to be delivered to the wrong session. After the
    # limit block and the reservation: a refused fire leaves no entry.
    _state._sessions.setdefault(session_id, {"created": True, "message_count": 0})
    _state._sessions[session_id]["is_task"] = True
    if kind.judge:
        # A check's judge session is never judged (CHECKS.md).
        _state._sessions[session_id]["check"] = True
    # Stamp last_active so a stub that leaks before _run_task's finally runs
    # (a pre-launch failure) is still ageable by reap_task_sessions.
    _state._sessions[session_id]["last_active"] = shared.now_iso()
    _state._save_sessions()

    try:
        await run_db(
            task_store.create_run,
            run_id, task.id, task.agent, trigger_type, trigger_source, final_prompt,
            task_type, task.scope, _run_creator(task), task.spender_sub,
        )

        # Create the run's chat row BEFORE the admission slot: a parked run
        # ("pending", waiting on host memory) must be visible — the dashboard
        # opens task-<run_id> immediately and needs the row + runs.chat_id to
        # show an honest "queued" state instead of "Chat not found". Worker
        # chats (target_chat_id) already exist from the spawn path.
        if not task.target_chat_id:
            await run_db(
                _create_task_chat_row, session_kind.task_chat_id(run_id), run_id, task,
            )

        # Read here, with no await before the launch: a fire may have waited
        # for its spawn slot since a drain began.
        if drain.holding() and not kind.judge:
            # A planned stop holds new work: the run stays ``pending`` with
            # its chat row, its id goes back to the caller, and the boot or
            # the drain's resume starts it (``run_recovery.start_pending_runs``).
            # It keeps the task's reservation, so a schedule that fires again
            # during the hold gets this run back instead of minting another.
            # A follow-up's session is its lane's, and stays.
            if session_id != task.continue_session:
                _state._sessions.pop(session_id, None)
                _state._save_sessions()
            logger.info("Task %s: run %s held: the platform is holding new work",
                        task.id, run_id)
        else:
            _launch_run(run_id, session_id, task, final_prompt, trigger_type,
                        trigger_source, attempt, trigger_payload)
    except Exception:
        # Launch failed before _run_task could release the reservation.
        if shared._active_task_ids.get(task.id) == run_id:
            shared._active_task_ids.pop(task.id, None)
        raise

    # One-time tasks: mark fired immediately
    if task.run_at or task.delay_seconds is not None:
        await run_db(task_store.set_dynamic_task_fired, task.id)

    return run_id


def _launch_run(run_id: str, session_id: str, task: shared.TaskDefinition, prompt: str,
                trigger_type: str, trigger_source: str | None, attempt: int,
                trigger_payload: dict | None = None, *, reopened: dict | None = None) -> None:
    """The run's task, registered so a Stop, the drain and the shutdown find
    it. ``reopened``: the run row of a run the platform cut, started again
    under its id."""
    t = asyncio.create_task(
        _run_task(run_id, session_id, task, prompt, trigger_type,
                  trigger_source, attempt, trigger_payload, reopened=reopened)
    )
    shared._running_tasks[run_id] = t
    t.add_done_callback(functools.partial(_launched_done, run_id, task))


def _launched_done(run_id: str, task: shared.TaskDefinition, t: asyncio.Task) -> None:
    """The run's task ended: out of the registry. A task cancelled before
    its first step never entered its frame (``_run_task``): nothing ended
    its row and nothing released its registries. The frame clears both
    cancel marks at its end whatever way it ends, so a mark that is still
    there on a cancelled task means the frame did not run to there: the
    run's end and its release are done here. A shutdown's cancel sets no
    mark: the run is left for the boot."""
    shared._running_tasks.pop(run_id, None)
    if t.cancelled() and (run_id in shared._user_cancelled_runs
                          or run_id in shared._platform_interrupts):
        spawn(_end_unentered(run_id, task), name="end_unentered_run", owner="runner")


async def _end_unentered(run_id: str, task: shared.TaskDefinition) -> None:
    """What the frame does for a run cancelled before its slot, for a task
    that never ran it: the row's end, then the release."""
    try:
        await _end_unstamped(run_id)
    finally:
        await _retire(run_id, task)


def reopen(run: dict, task: shared.TaskDefinition, prompt: str, *, fresh: bool = False) -> bool:
    """Start again a run the platform cut, under its own id: the registries
    a first fire sets (the task's one-run reservation, the session's task
    stamp, the running task), then the run's body as a continue round on its
    own session. ``fresh``: the run never started a session, so it runs its
    own prompt from the start, on the session its fire names (a follow-up
    keeps its lane's conversation), else a new one. False when the task runs
    already."""
    from core.session import session_state as _state
    run_id = run["id"]
    if task.id in shared._active_task_ids or run_id in shared._running_tasks:
        return False
    session_id = run.get("session_id") or ""
    if fresh or not _is_valid_session_uuid(session_id):
        fresh = True
        session_id = (task.continue_session if _is_valid_session_uuid(task.continue_session)
                      else str(uuid.uuid4()))
    shared._active_task_ids[task.id] = run_id
    _state._sessions.setdefault(session_id, {"created": True, "message_count": 0})
    _state._sessions[session_id]["is_task"] = True
    _state._sessions[session_id]["last_active"] = shared.now_iso()
    _state._save_sessions()
    if not fresh:
        task = task.model_copy(update={"continue_session": session_id})
    _launch_run(run_id, session_id, task, prompt, run.get("trigger_type") or "scheduled",
                run.get("trigger_source"), 1, None, reopened=None if fresh else run)
    return True


#: How long a reopen waits for the cut run's own task to end.
_UNWIND_WAIT_S = 60.0


async def unwound(run_id: str, *, timeout: float = _UNWIND_WAIT_S) -> None:
    """Wait for the task of ``run_id`` that still runs in this process to end
    (it left its run for the reopen and is releasing its registries), never
    cancelling it. Returns at once when there is none."""
    task = shared._running_tasks.get(run_id)
    if task is not None and not task.done():
        await asyncio.wait({task}, timeout=timeout)
        # The task's done callback takes it out of the registry.
        await asyncio.sleep(0)


def _run_creator(task: shared.TaskDefinition) -> str | None:
    """The run row's ``created_by``: the prompt's author. For a row the
    offboarding transfer moved that is the first creator, not the owner
    the row names now, so a re-warm of the run's chat resolves the
    knowledge-write provenance from the person who wrote the prompt."""
    return task.transferred_from or task.created_by


async def _run_task(run_id: str, session_id: str, task: shared.TaskDefinition, prompt: str,
                    trigger_type: str, trigger_source: str | None, attempt: int,
                    trigger_payload: dict | None = None, *,
                    reopened: dict | None = None) -> None:
    """The run's frame around ``_run_task_body``: the stream's replay buffer
    for exactly the run's lifetime, the end of a run its body's own handlers
    never saw (a cancel or a failure before admission — the pre-slot window
    and the parked slot — used to leave the row ``pending`` forever and the
    registries reserved), a one-time task's row retired with the run that
    ended it, and the registries released once, whatever way the run
    ended."""
    shared._run_event_buffer[run_id] = []
    try:
        await _run_task_body(run_id, session_id, task, prompt, trigger_type,
                             trigger_source, attempt, trigger_payload, reopened=reopened)
    except asyncio.CancelledError:
        await _end_unstamped(run_id)
        raise
    except Exception as e:
        logger.exception(
            "Task failed outside its run body: run=%s, task=%s: %s",
            run_id, task.id, e,
        )
        await _end_unstamped(run_id, error=str(e))
    finally:
        await _retire(run_id, task)


async def _retire(run_id: str, task: shared.TaskDefinition) -> None:
    """A run's registries released once, whatever way it ended, and a
    one-time task's row retired with the run that ended it."""
    shared._run_subscribers.pop(run_id, None)
    shared._run_event_buffer.pop(run_id, None)
    shared._user_cancelled_runs.discard(run_id)
    shared._platform_interrupts.pop(run_id, None)
    opened = run_id in shared._open_rounds
    shared._open_rounds.discard(run_id)
    # A one-time task's row goes with the run that ended it, whatever
    # way it ended (recurring and trigger tasks keep theirs) — unless
    # this attempt handed off to a retry, which re-reserved the task
    # under its own run id before this ran and owns the row from there,
    # or the run was left running for a restart, whose recovery reads
    # the row. Before the release below: a Run Now landing in between
    # is refused as a duplicate, never run on a vanishing row.
    left = run_id in shared._left_for_reopen
    shared._left_for_reopen.discard(run_id)
    if (task_kinds.self_removes(task)
            and shared._active_task_ids.get(task.id) in (run_id, None)
            and not left
            and not await _left_running_for_restart(run_id)):
        try:
            await asyncio.to_thread(task_store.delete_dynamic_task, task.id)
            shared._dynamic_task_ids.discard(task.id)
            logger.debug("Cleaned up fired one-time task: %s", task.id)
        except Exception:  # noqa: BLE001  # the task row's removal must not skip the reservation release
            pass  # non-critical, task was already marked fired
    if left and not opened:
        # A planned stop left the run ``pending`` before its round opened:
        # it keeps the task's reservation, as a fire held at its launch
        # does (``_execute_task``), so another fire of the task during the
        # hold gets this run back and mints none. The drain's resume or the
        # boot takes it (``run_recovery.start_pending_runs``).
        return
    # Release the dedup reservation — but only if it still points at THIS
    # run, so a retry hand-off (which re-reserved task.id under its own
    # run_id) keeps its reservation until it finishes.
    if shared._active_task_ids.get(task.id) == run_id:
        shared._active_task_ids.pop(task.id, None)


async def _left_running_for_restart(run_id: str) -> bool:
    """The shutdown path leaves a recoverable remote run ``running`` for the
    restart's recovery to finish; its task row must survive with it."""
    if not (shared._shutting_down or drain.stopping()):
        return False
    try:
        run = await asyncio.to_thread(task_store.get_run, run_id)
    except psycopg.Error:
        return True
    return bool(run) and run_status.is_live(cast("dict", run).get("status"))


async def _slot_target_of(task, ident) -> str:
    """Where a run's slot is taken: the builder's own decision (a judge's
    machine, the task's machine, then the resolver, then the compute machine
    that serves the platform's place, one that shares the platform's folder
    for a judge, for a call's wrap-up and for an agent whose tool reaches a
    local address, ``remote_status.keeps_beside``), so the slot counts where
    the run goes. A run of the platform's place waits for a compute machine, at most
    ``concurrency.NO_COMPUTE_WAIT_S``: with none after that its slot is
    refused (``placement.NO_COMPUTE``) and its build fails with the sentence,
    the run's one report. The same on a store error (the build inside the
    slot is authoritative)."""
    from core import concurrency
    from core.config.task_config_builder import task_target
    from core.session.session_manager import compute_placement_for
    from services.remote import remote_status
    shared = bool(getattr(task, "judge", None) or getattr(task, "wrapup", None))
    try:
        target = (await asyncio.to_thread(task_target, task, ident)).target
        if not placement.is_local(target):
            return target
        engine = await asyncio.to_thread(_task_layer, task)
        # The rule of a tool's local address, read once here: the wait below
        # asks its predicate on the loop at every sign-in.
        shared = shared or await asyncio.to_thread(remote_status.keeps_beside, getattr(task, "agent", "") or "")
    except psycopg.Error:
        return placement.NO_COMPUTE

    def _serves() -> bool:
        return compute_placement_for(engine, shared_folder=shared) is not None

    if not _serves():
        logger.info("Task %s waits for a compute machine (at most %.0f s)",
                    getattr(task, "id", "?"), concurrency.NO_COMPUTE_WAIT_S)
        await concurrency.wait_for_compute(_serves)
    return placement.admission_target(target, compute_placement_for(engine, shared_folder=shared))


async def _run_task_body(run_id: str, session_id: str, task: shared.TaskDefinition, prompt: str,
                         trigger_type: str, trigger_source: str | None, attempt: int,
                         trigger_payload: dict | None = None, *,
                         reopened: dict | None = None) -> None:
    from core.concurrency import task_slot
    from core.config.task_config_builder import (
        resolve_task_identity,
        task_allows_knowledge_rw,
    )
    from storage.pg import run_db

    # Resolve the execution target BEFORE entering the task slot so a REMOTE task
    # doesn't take a local-G slot or block on the local queue (it's bounded by its
    # own satellite). Cheap standalone resolve with the SAME identity inputs
    # build_task_agent_config uses (⇒ same target); the full build + status/persist
    # stay INSIDE the slot so a queued task is never prematurely marked "running".
    # On any error, fall back to local (worst case the slot over-counts by one;
    # the builder's resolve inside is authoritative for the actual run).
    # Resolve identity ONCE, outside the try, so creds_user_sub / role are in
    # scope both for the slot-target pre-resolve here AND the execution-layer
    # resolve inside the slot (below). The layer's per-user isolation guard
    # needs the real user_sub to tell a user-scope task on the user's OWN
    # machine apart from an agent-scope task that must never land on a
    # user-paired machine.
    _ident = await run_db(
        resolve_task_identity, task.agent, task.scope, task.created_by,
        allow_knowledge_rw=task_allows_knowledge_rw(task),
    )
    _slot_target = await _slot_target_of(task, _ident)

    async with task_slot(session_id, target=_slot_target, ring_key=task.created_by or ""):
        run = task_run.TaskRun(run_id, session_id, task, prompt, trigger_type, trigger_source,
                               attempt, trigger_payload, _ident, _slot_target, reopened=reopened)
        lanes.hold_report(*run.report_keys)

        try:
            await run.admitted()
        except task_run.LeftForReopen:
            # The platform's stop cut the run's turn before the cancel
            # reached it, or its machine came back without its session: the
            # row stays live, for the boot or for the continue pass, and the
            # frame keeps the task's row for the reopen.
            shared._left_for_reopen.add(run_id)
            logger.info("Run %s left %s (chat=%s)", run_id,
                        "running for its reopen" if run.round_opened
                        else "pending: the platform is holding new work", run.chat_id)
        except asyncio.CancelledError:
            if run.leave_for_recovery():
                raise
            await run.cancelled()
        except Exception as e:  # noqa: BLE001  # the run's last line: failed() logs it and ends the run
            await run.failed(e)
        finally:
            run.release()


def task_prelude(text: str, *, user_tz: str | None, parallel_line: str = "") -> str:
    """A task run's first prompt into a terminal: the time stamp and the
    parallel-activity line ahead of the text on the SAME line (the prelude
    matchers in ws/dashboard_chat_text.py end a prelude at spaces too). The
    Claude TUI journals a multi-line prompt, which reaches it as a bracketed
    paste, wrapped as pasted content; a single-line task prompt stays a
    typed line."""
    head = f"[Current time: {config.format_current_time(user_tz)}]"
    return " ".join(part for part in (head, parallel_line, text) if part)


async def _interactive_prelude(prompt: str, run_tz: str | None, session_id: str) -> str:
    """An interactive run's first prompt with the time line a dashboard turn
    carries (the terminal is handed it here). The parallel sessions come
    with the start's data, through the terminal's start hook or in its text
    (``start_context``), never twice."""
    return task_prelude(prompt, user_tz=run_tz)


def _notify_target(task: shared.TaskDefinition) -> str:
    """Who a task's page goes to: its creator for a personal task, the agent's
    people for an agent task."""
    return cast("str", task.created_by) if task.scope == "user" else task.agent


def _resume_username_needed(task: shared.TaskDefinition) -> str | bool | None:
    """A personal task with a creator resumes under that person's folder."""
    return task.scope == "user" and task.created_by


async def _end_run(run_id: str, status: str, **fields) -> None:
    """Stamp the row terminal, then tell the run stream. The ``done`` frame
    carries the word just written, so a subscriber that reads the row on
    ``done`` reads the row as the runner left it; every terminal write the
    scheduler task makes goes through here, and nothing else sends ``done``.
    ``done`` is the last frame: the buffer and the subscriber list retire
    with it, so a frame the closing CLI still yields reaches nobody."""
    await asyncio.to_thread(task_store.update_run, run_id, status=status, **fields)
    await _broadcast(run_id, {"type": "done", "status": status})
    shared._run_event_buffer.pop(run_id, None)
    shared._run_subscribers.pop(run_id, None)


async def _end_unstamped(run_id: str, *, error: str | None = None) -> None:
    """The end of a run its body never stamped: a cancel or a failure before
    the slot admitted it (the pre-slot window, the parked slot). A row past
    ``pending`` was stamped by the body's own handlers — or is the shutdown
    path's deliberately still-running run — and is left alone, with one
    exception. A run reopened under its id is ``running`` before its slot,
    and this frame opened no round of it: a failure there, a person's Stop
    and the platform's own stop (``scheduler.platform_cancel_run``) end it
    here, and its delegator is told once (``run_recovery.report_failed``,
    ``report_stopped``). A shutdown's cancel names no reason: that run stays
    for the boot."""
    try:
        run = await asyncio.to_thread(task_store.get_run, run_id)
        if not run:
            return
        if run.get("status") == run_status.RUNNING and run_id not in shared._open_rounds:
            await _end_reopened(run, error)
            return
        if run.get("status") != run_status.PENDING:
            return
        if error is None and shared._shutting_down and run_id not in shared._user_cancelled_runs:
            # The platform's stop: the run never started and stays pending
            # for the boot to start (``run_recovery.start_pending_runs``).
            return
        if error is not None:
            status, message = run_status.FAILED, error
        elif run_id in shared._user_cancelled_runs:
            status, message = run_status.CANCELLED, "Interrupted by user"
        else:
            status = run_status.FAILED
            message = shared._platform_interrupts.get(run_id) or "Cancelled while queued"
        await _end_run(run_id, status, error_message=message, completed_at=shared.now_iso())
    except Exception:
        logger.exception("pre-admission stamp failed for run %s", run_id)


async def _end_reopened(run: dict, error: str | None) -> None:
    """A reopened run ended before this frame opened its round, by what
    ended it, in the order a ``pending`` run's end reads: a failure, a
    person's Stop, the platform's stop with its reason. None of them: the
    row stays live."""
    from services.scheduler import run_recovery
    run_id = run["id"]
    why = error
    if why is None and run_id not in shared._user_cancelled_runs:
        why = shared._platform_interrupts.get(run_id)
    if why is not None:
        await _end_run(run_id, run_status.FAILED, error_message=why, completed_at=shared.now_iso())
        await run_recovery.report_failed(run, why)
    elif run_id in shared._user_cancelled_runs:
        await _end_run(run_id, run_status.CANCELLED, error_message="Interrupted by user",
                       completed_at=shared.now_iso())
        await run_recovery.report_stopped(run)


async def _broadcast(run_id: str, event: dict) -> None:
    # The replay buffer for a late subscriber (a page refresh mid-run) lives
    # exactly as long as the run (_run_task): a frame after the run ended —
    # the closing CLI's trailing text — has nobody left to reach and is
    # dropped, never buffered.
    buf = shared._run_event_buffer.get(run_id)
    if buf is None:
        return
    buf.append(event)
    for q in list(shared._run_subscribers.get(run_id, [])):
        try:
            await q.put(event)
        except RuntimeError as e:
            logger.debug("run event broadcast: %s", e)
