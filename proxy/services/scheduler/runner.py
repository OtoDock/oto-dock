"""The task runner: ``_fire_task`` (the APScheduler job entry), ``_execute_task``
and ``_run_task`` (mutually recursive through the retry), the admission slot,
the run's chat row and the run-event broadcast.

One piece of the task scheduler; ``services/scheduler/scheduler.py`` is the
facade that assembles them and holds the registry API. Every ``core.*`` and
service import stays function-local so the standalone scheduler can import
this package without the platform.
"""

import asyncio
import contextlib
import json
import logging
import time
import uuid
from datetime import datetime, timezone


import config
from storage.agents import agent_store
from storage import database as task_store
from services.scheduler import delivery, firing, interactive, lanes, shared

logger = logging.getLogger("claude-proxy.scheduler")


async def _fire_task(task: shared.TaskDefinition) -> None:
    """APScheduler calls this as an async job. Creates a fire-and-forget task."""
    asyncio.create_task(_execute_task(task, trigger_type="scheduled"))


def _determine_task_type(task: shared.TaskDefinition, trigger_type: str) -> str:
    """Classify a task run — the delegate marker is EXPLICIT (stamped by the
    delegation spawn), never derived from use_persistent: the spawn cap and
    the runs-listing split count on it, and a scheduled persistent task must
    not read as a delegate."""
    if task.task_type == "delegate":
        return "delegate"
    if trigger_type == "triggered":
        return "trigger"
    if task.schedule or task.interval_seconds is not None:
        return "scheduled"
    return "one-time"


def _is_valid_session_uuid(v) -> bool:
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
    dock's lane graph; plain runs use the ``task::`` owner convention.

    The row is stamped with the lane this run will ACTUALLY use — the task's
    override when it has one, else the agent default. Billing reads the model
    off the chat row (``services/execution/stream_pump.py`` →
    ``usage_records``), so stamping the default here would mis-attribute every
    overridden run's tokens to the wrong model, and the chat header would lie
    about what answered."""
    task_path = task.override_execution_path or ""
    # Layer-aware (mirrors task_config_builder's wire resolution): with a
    # layer override, the agent-default model may be foreign to the run's
    # engine — stamping it would 400 the provider AND mis-attribute usage.
    task_model = (task.override_model
                  or config.get_cli_model(task.agent, layer=task_path or None))
    if task.task_type == "delegate":
        worker_sub = (task.created_by if task.scope == "user"
                      else f"agent::{task.agent}")
        task_store.create_chat(chat_id, worker_sub, task.agent, "auto",
                               model=task_model, execution_path=task_path,
                               origin="delegated",
                               parent_chat_id=task.parent_chat_id or "",
                               project_id=task.project_id or "",
                               delegate_role="worker",
                               title=task.name)
    else:
        user_sub = (task.created_by if task.scope == "user"
                    else f"task::{task.agent}")
        task_store.create_chat(chat_id, user_sub, task.agent, "auto",
                               model=task_model, execution_path=task_path)
    task_store.update_run(run_id, chat_id=chat_id)


def _task_layer(task: shared.TaskDefinition) -> str:
    """The engine this run will use: the task's override, else the agent's
    (the rule task_config_builder applies at spawn)."""
    return (task.override_execution_path
            or (agent_store.get_agent(task.agent) or {}).get("execution_path")
            or "claude-code-cli")


async def _pool_cap_block(task: shared.TaskDefinition) -> str:
    """The subscription pool cap on this run's engine and scope
    (``services.billing.pool_caps``): the reason when it blocks, else "".
    ``continue`` lets the run proceed when a key is there to continue on
    (the pool applies the exclusion at spawn); a task on a user pool with
    no creator has no pool to read."""
    from core.session.visibility import is_shared_only
    from services.billing import pool_caps
    from services.engines import subscription_pool
    # The pool the spawn draws on, not the scope the row stores: a task on a
    # shared-only agent runs agent-scoped whatever its row says
    # (task_config_builder.resolve_task_identity), so its seat comes from
    # the platform pool and the creator's own cap has no say.
    if task.scope == "user" and not await asyncio.to_thread(is_shared_only, task.agent):
        if not task.created_by:
            return ""
        scope, target = "user", task.created_by
    else:
        scope, target = "platform", ""
    layer = await asyncio.to_thread(_task_layer, task)
    cap = await asyncio.to_thread(pool_caps.evaluate, scope, target, layer)
    if cap.allowed:
        return ""
    if cap.on_reached == "continue" and await asyncio.to_thread(
        subscription_pool.cap_continue_available, layer, target or None,
    ):
        return ""
    logger.warning(f"Task {task.id} blocked by the {scope} pool cap: {cap.hit_text()}")
    return cap.short_reason()


async def _execute_task(task: shared.TaskDefinition, trigger_type: str = "scheduled",
                        trigger_source: str | None = None,
                        prompt_override: str | None = None, attempt: int = 1,
                        trigger_payload: dict | None = None) -> str:
    from core.session import session_state as _state

    # Continuations are wake deliveries, not LLM task runs — no run row, no
    # slot, no usage pre-check (the driven turn bills like any chat turn).
    if task.task_type == "continuation":
        return await firing._fire_continuation(task)

    # Scheduled fires only: manual Run-Now has a human waiting and event
    # triggers should react immediately — neither is spaced.
    if trigger_type == "scheduled":
        await firing._spawn_spacing_gate(task.id)

    run_id = f"run-{uuid.uuid4().hex[:12]}"
    # Session ID must be a valid UUID — Claude Code validates this for both
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
    final_prompt = prompt_override or task.prompt

    # Mark as task session so /v1/session/current excludes it.
    # Without this, task sessions (which share the agent name and have more recent
    # last_active) would be returned instead of the main interactive session,
    # causing callbacks to be delivered to the wrong session.
    _state._sessions.setdefault(session_id, {"created": True, "message_count": 0})
    _state._sessions[session_id]["is_task"] = True
    # Stamp last_active so a stub that leaks before _run_task's finally runs
    # (a pre-launch failure) is still ageable by reap_task_sessions.
    _state._sessions[session_id]["last_active"] = shared.now_iso()
    _state._save_sessions()
    if task.extra_context:
        final_prompt += "\n\n" + "\n\n".join(task.extra_context)

    # ── Usage limit check before execution ──
    try:
        from services.billing import usage_service
        blocked_by = ""
        if task.scope == "user" and task.created_by:
            creator = await asyncio.to_thread(task_store.get_user, task.created_by)
            creator_role = (creator or {}).get("role", "member")
            limit_status = await asyncio.to_thread(
                usage_service.check_user_limit, task.created_by, creator_role
            )
            if not limit_status["allowed"]:
                logger.warning(f"Task {task.id} blocked by user limit: {task.created_by}")
                blocked_by = "User usage limit exceeded"
        elif task.scope == "agent":
            limit_status = await asyncio.to_thread(
                usage_service.check_agent_limit, task.agent
            )
            if not limit_status["allowed"]:
                logger.warning(f"Task {task.id} blocked by agent limit: {task.agent}")
                blocked_by = "Agent usage limit exceeded"
        if not blocked_by:
            blocked_by = await _pool_cap_block(task)
        if blocked_by:
            task_type = _determine_task_type(task, trigger_type)
            await asyncio.to_thread(
                task_store.create_run, run_id, task.id, task.agent,
                trigger_type, trigger_source, final_prompt, task_type,
                task.scope, task.created_by,
            )
            await asyncio.to_thread(
                task_store.update_run, run_id,
                status="limit_exceeded",
                error_message=blocked_by,
                completed_at=datetime.now(timezone.utc).isoformat(),
            )
            return run_id
    except Exception as e:
        logger.error(f"Usage limit check failed for task {task.id}: {e}")

    task_type = _determine_task_type(task, trigger_type)

    # Dedup guard (general, every task type): never run the same task concurrently.
    # Reject a duplicate only on the first attempt; a retry (attempt > 1) is a
    # deliberate continuation so it re-reserves without the reject check. The check
    # + reserve are adjacent (no await between) → race-free on the event loop.
    # Released in _run_task's finally, guarded on run_id so a retry hand-off doesn't
    # release its successor's reservation. Logged, never a silent drop. (The
    # delegate "3 runs" cascade is fixed by reliable terminal delivery — each
    # delegate_task mints a distinct task_id — so this targets scheduled+manual
    # collisions / standalone-retry double-fires.)
    if attempt == 1 and task.id in shared._active_task_ids:
        logger.warning(
            f"Skipping duplicate concurrent run for task {task.id}: "
            f"run {shared._active_task_ids[task.id]} still active"
        )
        return shared._active_task_ids[task.id]
    shared._active_task_ids[task.id] = run_id

    try:
        await asyncio.to_thread(
            task_store.create_run,
            run_id, task.id, task.agent, trigger_type, trigger_source, final_prompt,
            task_type, task.scope, task.created_by,
        )

        # Create the run's chat row BEFORE the admission slot: a parked run
        # ("pending", waiting on host memory) must be visible — the dashboard
        # opens task-<run_id> immediately and needs the row + runs.chat_id to
        # show an honest "queued" state instead of "Chat not found". Worker
        # chats (target_chat_id) already exist from the spawn path.
        if not task.target_chat_id:
            await asyncio.to_thread(
                _create_task_chat_row, f"task-{run_id}", run_id, task,
            )

        t = asyncio.create_task(
            _run_task(run_id, session_id, task, final_prompt, trigger_type,
                      trigger_source, attempt, trigger_payload)
        )
        shared._running_tasks[run_id] = t
        t.add_done_callback(lambda _: shared._running_tasks.pop(run_id, None))
    except Exception:
        # Launch failed before _run_task could release the reservation.
        if shared._active_task_ids.get(task.id) == run_id:
            shared._active_task_ids.pop(task.id, None)
        raise

    # One-time tasks: mark fired immediately
    if task.run_at or task.delay_seconds is not None:
        await asyncio.to_thread(task_store.set_dynamic_task_fired, task.id)

    return run_id


@contextlib.asynccontextmanager
async def _admitted_slot(session_id: str, target: str, run_id: str):
    """``task_slot`` admission that stamps a still-``pending`` run terminal on
    cancellation. A cancel while PARKED on the slot escapes ``_run_task``'s
    inner CancelledError handler (it lives inside the slot body), leaving the
    row 'pending' forever — invisible to recovery until restart, and it
    409-blocks deleting the run chat's history. The status guard keeps every
    in-body cancel path (already stamped by the inner handler, or the
    shutdown path that deliberately leaves the run 'running') intact."""
    from core.concurrency import task_slot
    try:
        async with task_slot(session_id, target=target):
            yield
    except asyncio.CancelledError:
        try:
            run = await asyncio.to_thread(task_store.get_run, run_id)
            if run and run.get("status") == "pending":
                user_stop = run_id in shared._user_cancelled_runs
                await asyncio.to_thread(
                    task_store.update_run, run_id,
                    status="cancelled" if user_stop else "failed",
                    error_message="Interrupted by user" if user_stop
                    else "Cancelled while queued",
                    completed_at=shared.now_iso(),
                )
        except Exception:
            logger.exception(f"pending-cancel stamp failed for run {run_id}")
        raise


async def _run_task(run_id: str, session_id: str, task: shared.TaskDefinition, prompt: str,
                    trigger_type: str, trigger_source: str | None, attempt: int,
                    trigger_payload: dict | None = None) -> None:
    from core.session import session_state as _state
    from core.session.session_manager import get_execution_layer
    from core.config.task_config_builder import (
        build_task_agent_config, resolve_task_identity, task_allows_knowledge_rw,
    )
    from core.events.task_producer import task_produce
    from core.events.stream_pump import ChatStreamPump, _active_pumps
    from core.session.session_state import get_permission_queue
    from storage import remote_store

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
    _ident = resolve_task_identity(
        task.agent, task.scope, task.created_by,
        allow_knowledge_rw=task_allows_knowledge_rw(task.task_type),
    )
    # Exactly-once result delivery per run: the success path sets this right
    # after its _deliver_task_result; the cancel/exception handlers then skip
    # THEIR delivery (a failure after the result went out — e.g. session close
    # raising — used to deliver the same result a second time).
    result_delivered = False
    try:
        _slot_target = (await asyncio.to_thread(
            remote_store.resolve_execution_target, task.agent,
            _ident.creds_user_sub, _ident.role,
        ))[0]
    except Exception:
        _slot_target = "local"

    async with _admitted_slot(session_id, _slot_target, run_id):
        await asyncio.to_thread(
            task_store.update_run, run_id,
            status="running",
            started_at=shared.now_iso(),
        )
        start = time.monotonic()
        chat_id = task.target_chat_id or f"task-{run_id}"
        output_cursor = 0  # pre-run message cursor; set in step 1
        prompt_row_id = 0  # the driven prompt's own row; excluded from collection
        layer = None  # bound in step 2; guards the except cleanup if config build fails
        pump = None  # bound in step 6 (headless only); step 7 reads last_error
        # Set when a graceful abort leaves the session warm (step 7): the
        # finally must then keep its session-index entry so the idle reaper
        # + /v1/session/current still see the live session.
        session_kept_warm = False
        # Set when the step-1 lane gate had to reap a wedged prior pump: the
        # round must then respawn, never reuse (see _settle_prior_lane).
        lane_reaped = False
        # Seat accounting for the except path: the config build acquires a
        # pool seat that only start_session's bind lets a close release. A
        # round that failed after the build but before the bind (offline
        # target, spawn failure) must give the seat back itself.
        agent_cfg = None
        reused_warm = False
        session_started = False
        seat_released = False  # the offline-target branch returns it itself

        try:
            # 1. Persist the chat row + the user's prompt FIRST — BEFORE the slow
            #    spawn (config build / start_session / offline-target hard-fail) — so
            #    a pre-turn failure still shows the prompt and is visible in
            #    TaskRunView instead of "No messages yet." General task robustness:
            #    applies to every task type (scheduled / one-time / delegate).
            if task.target_chat_id:
                # Worker chat (surface="chat") — the spawn path created the row
                # with owner/origin/parent stamped. A missing row means the
                # user deleted the chat between spawn and fire.
                if not task_store.get_chat(chat_id):
                    raise RuntimeError("The worker chat for this run no longer exists")
                # BEFORE the output cursor below: a reap flushes the prior
                # round's blocks, and those rows must not be collected as
                # this run's output. Healthy live turns are WAITED out, not
                # reaped (a reap cancels the user's in-flight turn).
                lane_reaped = await lanes._settle_prior_lane(chat_id, run_id)
            elif not task_store.get_chat(chat_id):
                # Backstop: _execute_task pre-creates the row before the slot
                # (queued visibility); this covers direct _run_task callers
                # and a chat deleted while the run was parked.
                _create_task_chat_row(chat_id, run_id, task)
            # Clear stale abort flags from a PRIOR round on this chat —
            # scheduler-driven turns don't run the dashboard's turn-start clear,
            # and the post-run user_interrupted check keys on these flags (a
            # stale graceful=True from a prior round must not suppress a
            # close after THIS round's non-graceful abort).
            task_store.update_chat(
                chat_id, session_id=session_id,
                last_turn_aborted=False, last_abort_graceful=False,
            )
            # Cursor BEFORE this round's prompt lands: output collection (and
            # the failure-path collection) reports only what THIS run produced.
            output_cursor = task_store.get_last_chat_message_id(chat_id)
            # The user-prompt bubble is persisted AFTER the config build below, so
            # we can skip it for an interactive run (there the prompt rides the
            # PTY/argv and the transcript tailer backfills it — pre-persisting
            # would duplicate it). Config build is fast relative to start_session,
            # so the prompt still lands before the slow spawn.
            # chat_id on the run immediately so the dashboard can load it while running.
            await asyncio.to_thread(
                task_store.update_run, run_id, chat_id=chat_id, session_id=session_id,
            )

            # 2. Build config via task_config_builder (handles scope creds,
            #    security context, task suffix, env vars). trigger_payload
            #    threads through so manifest agent_context blocks resolve
            #    ${trigger.*} tokens for webhook-fired tasks.
            agent_cfg = await build_task_agent_config(
                task.agent, task, session_id,
                trigger_payload=trigger_payload,
            )

            # Hard-fail if the resolved target is the offline sentinel from
            # the resolver. Without this, agent-level remote targets silently
            # fall back to local CLI when the satellite is unreachable, which
            # masks misconfigurations and produces wrong results (different
            # MCPs, different filesystem). Mirrors ws/dashboard.py warmup.
            from core.config.config_builder import (
                is_hard_fail_target, extract_offline_machine, release_config_seat,
            )
            from storage import remote_store
            if is_hard_fail_target(agent_cfg.execution_target):
                # Every branch below raises — the build's seat goes back now,
                # once: the except's release is skipped for it.
                release_config_seat(session_id, agent_cfg)
                seat_released = True
                offline_machine_id = extract_offline_machine(agent_cfg.execution_target)
                machine = remote_store.get_remote_machine(offline_machine_id)
                if not machine:
                    # Deleted mid-flight (the delete's bulk chat transition
                    # races this run). The row is already fixed — the next
                    # run fresh-resolves and auto-continues.
                    raise RuntimeError(
                        "This task's remote machine no longer exists — "
                        "the next run will use the agent's current target."
                    )
                machine_label = machine.get("name") or offline_machine_id[:8]
                is_admin_target = (machine.get("pairing_scope") or "") == "admin"
                if is_admin_target:
                    raise RuntimeError(
                        f"This agent's remote machine '{machine_label}' is currently offline. "
                        f"Please reconnect the remote machine or contact your admin."
                    )
                raise RuntimeError(
                    f"Your remote machine '{machine_label}' is offline. "
                    f"Reconnect it from User Settings → Remote Machines, or remove the per-agent override."
                )

            # Resolve the execution layer from the config's already-resolved
            # target + path so the layer can never disagree with the config
            # (the divergence that silently ran user-scoped tasks on local).
            layer = get_execution_layer(
                task.agent,
                execution_path=agent_cfg.execution_path,
                user_sub=_ident.creds_user_sub,
                role=_ident.role,
                execution_target=agent_cfg.execution_target,
            )

            # Pin the task chat to the target it actually runs on
            # — same affinity as dashboard chats (ws/dashboard.py warmup pin).
            # Makes TaskRunView continues resume on the origin machine, and
            # lets delete_remote_machine transition task chats to
            # auto-continue like any other chat.
            task_store.update_chat(
                chat_id, execution_target=agent_cfg.execution_target,
            )

            # Pass chat_id to meetings-mcp so it uses the task's chat
            # (/v1/session/current excludes task sessions, returning the
            # wrong chat_id for meetings started from tasks).
            agent_cfg.extra_env["MEETINGS_MCP_CHAT_ID"] = chat_id
            agent_cfg.extra_env["MEETINGS_MCP_SESSION_ID"] = session_id
            agent_cfg.extra_env["NOTIF_MCP_CHAT_ID"] = chat_id

            # For continue_session (multi-turn delegation), prefer driving the
            # turn on the still-warm session; otherwise the session has prior
            # conversation on disk — use --resume to preserve context.
            # Same UUID guard as _execute_task: bad data falls back to fresh
            # session rather than passing "--resume false" to the CLI.
            reused_warm = False
            if _is_valid_session_uuid(task.continue_session):
                # Final lane gate at the decision point: the config build did
                # DB/thread work since the step-1 gate — a user turn may have
                # started on the lane meanwhile. A quiet observation here
                # means the respawn path can't hit the satellite stale-live
                # guard mid-turn and the pump registration won't clobber a
                # live dashboard pump (post-gate races on the reuse path are
                # serialized by layer.session_lock).
                _prior = _active_pumps.get(chat_id)
                if _prior is not None and not _prior.is_done:
                    await lanes._await_lane_quiescence(chat_id)
                    # The wait may have let a user turn land rows/flags —
                    # re-take the output cursor + re-clear the abort flags so
                    # that turn is neither collected as this run's output nor
                    # mis-promoted to user_interrupted.
                    task_store.update_chat(
                        chat_id,
                        last_turn_aborted=False, last_abort_graceful=False,
                    )
                    output_cursor = task_store.get_last_chat_message_id(chat_id)
                if not lane_reaped:
                    reused_warm = await lanes._try_reuse_warm_session(
                        layer, agent_cfg, session_id, run_id,
                    )
                if reused_warm:
                    # No spawn: the turn rides the warm session (dashboard
                    # turn-2+ semantics; spawn-time binding/context stand).
                    # Release the seat this round's config build acquired —
                    # nothing will bind it — and force headless: the
                    # force-headless below keys on agent_cfg.resume, which
                    # reuse never sets.
                    agent_cfg.interactive = False
                    # keep_binding: the id is the LIVE warm session's, bound
                    # by the round that spawned it — only this round's seat
                    # (and its acquire-window claim) goes back.
                    release_config_seat(session_id, agent_cfg, keep_binding=True)
                else:
                    # Resumability pre-check (same gate as dashboard warmups
                    # and the one-shot delivery): a blind --resume on a
                    # missing conversation makes the CLI's "No conversation
                    # found with session ID: …" the run's entire output. Fall
                    # back to a fresh session in the SAME chat instead — a
                    # fresh id, so a half-present file under the old one
                    # can't collide.
                    resume_username = ""
                    if task.scope == "user" and task.created_by:
                        resume_username = task_store.get_username_by_sub(task.created_by) or ""
                    if await layer.can_resume_session(
                        session_id, agent_name=task.agent, username=resume_username,
                    ):
                        agent_cfg.resume = True
                    else:
                        old_sid = session_id
                        session_id = str(uuid.uuid4())
                        _state._sessions[session_id] = _state._sessions.pop(
                            old_sid, {"created": True, "message_count": 0},
                        )
                        # The dead session's binding would outlive it: nothing
                        # else releases a seat under an id no session runs.
                        from services.engines import subscription_pool
                        subscription_pool.release_subscription(old_sid)
                        task_store.update_chat(chat_id, session_id=session_id)
                        await asyncio.to_thread(
                            task_store.update_run, run_id, session_id=session_id,
                        )
                        logger.warning(
                            f"Delegate continue: session {old_sid[:8]} has no resumable "
                            f"conversation on agent={task.agent} — fresh session "
                            f"{session_id[:8]} in the same chat"
                        )

            # Interactive task prep. Only FRESH interactive spawns run
            # interactive — a continue_session RESUME stays headless
            # (resuming would also make the tailer replay prior turn-end
            # signals). The frontend resolves the terminal on re-open from the
            # chat's stored execution_mode, so pin it.
            first_prompt_in_argv = False
            if agent_cfg.interactive and agent_cfg.resume:
                agent_cfg.interactive = False
            if agent_cfg.interactive:
                # The interactive session is keyed to the run chat (the codex/cli
                # layers pass chat_id=config.chat_id to interactive_session.register).
                # task_config_builder doesn't set it (the headless pump gets chat_id
                # directly), so set it here — without it the transcript tailer
                # short-circuits on an empty chat_id and the completion watcher never
                # fires (the run hangs) + nothing persists.
                agent_cfg.chat_id = chat_id
                task_store.update_chat(chat_id, execution_mode="interactive")
                # Codex delivers the cold prompt via its launch argv (auto-runs
                # after MCP warm); Claude gets it via the PTY flush in the watcher.
                if (agent_cfg.execution_path or "") == "codex-cli":
                    agent_cfg.interactive_first_prompt = prompt
                    first_prompt_in_argv = True

            # Persist the user-prompt bubble for the message-list view — but NOT
            # for interactive runs (the tailer backfills it from the transcript,
            # like an interactive chat; pre-persisting would duplicate it).
            if not agent_cfg.interactive:
                delegating_slug = task.on_complete_agent
                if delegating_slug:
                    delegating_data = agent_store.get_agent(delegating_slug)
                    prompt_agent_meta = {
                        "agent_slug": delegating_slug,
                        "agent_display_name": delegating_data["display_name"] if delegating_data else delegating_slug,
                        "agent_color": delegating_data.get("color", "") if delegating_data else "",
                        "badge": "delegated by",
                    }
                else:
                    executing_data = agent_store.get_agent(task.agent)
                    prompt_agent_meta = {
                        "agent_slug": task.agent,
                        "agent_display_name": executing_data["display_name"] if executing_data else task.agent,
                        "agent_color": executing_data.get("color", "") if executing_data else "",
                        "badge": "task prompt",
                    }
                prompt_row_id = task_store.add_chat_message(
                    chat_id, "user", prompt,
                    event_data=json.dumps(prompt_agent_meta),
                )

            # 3. Start session via execution layer (skipped when the round
            #    rides an already-warm session — see _try_reuse_warm_session)
            if not reused_warm:
                await layer.start_session(session_id, agent_cfg)
            session_started = True

            # 4. Mark as task session so /v1/session/current excludes it.
            _state._sessions.setdefault(session_id, {"created": True, "message_count": 0})
            _state._sessions[session_id].update({
                "is_task": True,
                "client_type": "task",
                "agent": task.agent,
                "last_active": shared.now_iso(),
            })
            _state._save_sessions()

            # 5/6. Drive the turn to completion.
            if agent_cfg.interactive:
                # Interactive task: NO pump/producer — a PTY emits bytes, not
                # CommonEvents. Inject the first prompt (Claude via PTY;
                # Codex rode the launch argv) and wait for the turn-end signal:
                # the transcript/rollout tailer fires interactive_session's
                # on_turn_complete (gated bg-empty + min-turn-time). The tailer
                # also persists the turns to chat_messages, so the shared
                # output-collection + update_run + delivery below Just Work.
                await interactive._run_interactive_task(
                    session_id, chat_id, prompt, first_prompt_in_argv,
                )
            else:
                # 5. Create event queue + launch producer
                event_queue: asyncio.Queue = asyncio.Queue()
                producer = asyncio.create_task(
                    task_produce(
                        layer, session_id, prompt, event_queue, run_id,
                        broadcast_fn=_broadcast, settle_timeout=30.0,
                    )
                )

                # 6. Create pump, register in _active_pumps, and run to completion.
                # Registration allows dashboard WS to attach for live streaming.
                perm_queue = get_permission_queue(session_id)
                pump = ChatStreamPump(
                    chat_id=chat_id,
                    session_id=session_id,
                    producer=producer,
                    event_queue=event_queue,
                    perm_queue=perm_queue,
                    scope=task.scope,
                    source_type="task",
                )
                _active_pumps[chat_id] = pump
                pump.start()
                # Run to completion under the stall watchdog (reaps a wedged
                # turn instead of holding the run "generating" forever).
                await lanes._watch_task_pump(layer, pump, run_id, chat_id, session_id)

            # 6b. If the agent started a meeting, wait for it to conclude.
            # The meeting pump starts after the task pump clears (orchestrator
            # checks is_done). We poll the DB for the meeting status.
            active_meeting = await asyncio.to_thread(
                task_store.get_active_meeting_for_chat, chat_id
            )
            completed_meeting_id: str | None = None
            if active_meeting:
                completed_meeting_id = active_meeting["id"]
                logger.info(f"Task {run_id}: waiting for meeting {completed_meeting_id} to conclude")
                meeting_wait_start = time.monotonic()
                while True:
                    await asyncio.sleep(2)
                    m = await asyncio.to_thread(task_store.get_meeting, completed_meeting_id)
                    if not m or m["status"] in ("concluded", "failed"):
                        break
                    if time.monotonic() - meeting_wait_start > 600:
                        logger.warning(f"Task {run_id}: meeting {completed_meeting_id} wait timed out after 10min")
                        break
                logger.info(f"Task {run_id}: meeting {completed_meeting_id} finished")

            # 7. Read cost from chat record (pump saved it)
            chat_row = task_store.get_chat(chat_id)
            task_total_cost = (chat_row or {}).get("total_cost", 0) or 0
            # A user watching the lane pressed Stop mid-turn (WS abort set the
            # flag; step 1 cleared any stale one). The run itself completed —
            # partial output is persisted — but the delegating agent must see
            # "user_interrupted", not a clean completion.
            final_status, session_kept_warm = lanes._post_run_session_action(chat_row)
            if final_status == "user_interrupted" and layer is not None:
                if session_kept_warm:
                    logger.info(
                        f"Task {run_id}: graceful abort — keeping session "
                        f"{session_id[:8]} warm on lane chat {chat_id[:8]}"
                    )
                else:
                    try:
                        await layer.close_session(session_id)
                    except Exception:
                        logger.debug(
                            f"Task {run_id}: post-abort close_session failed",
                            exc_info=True,
                        )

            # 8. Collect output text for the on-complete delivery payload.
            # For meetings: use the moderator's final summary (not the entire
            # transcript which would overwhelm the parent with redundant text).
            if completed_meeting_id:
                meeting_data = await asyncio.to_thread(task_store.get_meeting, completed_meeting_id)
                turns = await asyncio.to_thread(task_store.get_meeting_turns, completed_meeting_id)
                moderator = (meeting_data or {}).get("moderator", "")
                mod_turns = [t for t in (turns or []) if t.get("agent") == moderator and t.get("content")]
                final_output = mod_turns[-1]["content"] if mod_turns else lanes._collect_task_output(chat_id, output_cursor)
            else:
                final_output = lanes._collect_task_output(chat_id, output_cursor)

            # A run killed by a provider usage limit streams the limit notice
            # as a clean result text — without this check it lands as a
            # deceptive `completed` with the notice as output.
            limit_line = (
                lanes._limit_notice(final_output) if final_status == "completed" else None
            )
            if limit_line:
                final_status = "failed"
                logger.warning(
                    f"Task run {run_id} stopped on a provider usage limit: {limit_line}"
                )

            # A run whose stream DIED on an engine/provider error (claude
            # is_error result — e.g. "Not logged in"; codex turn failure —
            # e.g. a cross-engine model 400) used to land as a deceptive
            # `completed` with empty (or error-text) output and $0 cost.
            # The pump breaks its loop on the ERROR event and records the
            # message — terminal by construction, so consult it exactly like
            # the limit notice. Empty output alone NEVER fails a run.
            run_error = None
            if final_status == "completed" and not limit_line:
                run_error = (getattr(pump, "last_error", "") or "").strip() or None
                if run_error:
                    final_status = "failed"
                    logger.warning(
                        f"Task run {run_id} failed on an engine error: "
                        f"{run_error[:300]}"
                    )

            duration_ms = int((time.monotonic() - start) * 1000)
            await asyncio.to_thread(
                task_store.update_run, run_id,
                status="failed" if (limit_line or run_error) else "completed",
                error_message=(
                    f"Provider usage limit: {limit_line}" if limit_line
                    else (run_error[:2000] if run_error else None)
                ),
                output_text=final_output[:10000] if final_output else "",
                completed_at=shared.now_iso(),
                duration_ms=duration_ms,
                session_id=session_id,
                cost_usd=task_total_cost if task_total_cost > 0 else None,
                chat_id=chat_id,
            )
            logger.info(
                f"Task completed: run={run_id}, task={task.id}, duration={duration_ms}ms"
            )
            # Shared-library turn-end reconcile (see ChatStreamPump): a worker
            # or scheduled run that deleted/wrote inside a library in its
            # sandbox gets it propagated now, not at the next 5-min sweep.
            try:
                from services.knowledge import library_projector
                library_projector.schedule_reconcile_for_agent(task.agent)
            except Exception:
                pass
            if run_error and not final_output:
                # Pre-output engine failure: deliver a clearly-marked
                # NON-EMPTY terminal so a delegating agent sees the failure
                # (not an empty bubble) and doesn't re-delegate — twin of
                # the exception path's fail_output synthesis.
                final_output = (
                    f'⚠ Task "{task.name}" failed on an engine error: '
                    f'{run_error[:300]}'
                )
            await delivery._deliver_task_result(
                task, final_status, final_output,
                worker_chat_id=chat_id, output_cursor=output_cursor,
                prompt_row_id=prompt_row_id, prompt_text=prompt,
            )
            result_delivered = True

            # Fire completion notification ONLY for 'auto' mode. 'manual' agents
            # fire their own (system-injected prompt tells them to); 'none' is
            # fully silent. A usage-limited run warns instead for every mode but
            # 'none' — the agent that would have self-notified in 'manual' died
            # with the limit.
            if limit_line or run_error:
                # Engine/limit failure: the agent that would have
                # self-notified in 'manual' mode died with the error, so warn
                # for every mode but 'none'.
                if task.notification_mode != "none":
                    from services.notifications import notification_manager
                    asyncio.create_task(notification_manager.fire_notification(
                        title=(f"Task stopped: usage limit — {task.name}"
                               if limit_line else f"Task failed: {task.name}"),
                        body=(limit_line or run_error or "")[:200],
                        severity="warning",
                        scope=task.scope,
                        target=task.created_by if task.scope == "user" else task.agent,
                        source="task",
                        source_id=task.id,
                        agent_slug=task.agent,
                        chat_id=chat_id,
                    ))
            elif task.notification_mode == "auto":
                from services.notifications import notification_manager
                asyncio.create_task(notification_manager.fire_notification(
                    title=f"Task Complete: {task.name}",
                    body=final_output[:200] if final_output else "Task finished.",
                    severity=task.notify_severity,
                    scope=task.scope,
                    target=task.created_by if task.scope == "user" else task.agent,
                    source="task",
                    source_id=task.id,
                    agent_slug=task.agent,
                    chat_id=chat_id,
                ))

            # Interactive task: close the PTY session now the run is terminal
            # (frees the slot; the transcript is already persisted). Headless tasks
            # let their pump-session idle-reap. No-op for a non-interactive run.
            await interactive._close_interactive_task_session(session_id)

        except asyncio.CancelledError:
            user_stop = run_id in shared._user_cancelled_runs
            platform_reason = shared._platform_interrupts.pop(run_id, None)
            # Graceful shutdown of a re-adoptable remote turn: LEAVE the run
            # running and DON'T close the satellite session — the CLI keeps
            # working, and after restart defer_orphaned_runs parks it +
            # sessions_alive re-adopts it (Mode C). Suppress the pump's
            # durable flush so the recovery replay doesn't duplicate content.
            from services.scheduler import run_recovery
            if (shared._shutting_down and not user_stop and platform_reason is None
                    and run_recovery.is_recovery_eligible(chat_id)):
                from core.events.stream_pump import suppress_recovery_flush
                suppress_recovery_flush(chat_id)
                logger.info(
                    f"Shutdown: leaving recoverable run {run_id} running "
                    f"for satellite re-adopt (chat={chat_id})"
                )
                raise
            if user_stop:
                await asyncio.to_thread(
                    task_store.update_run, run_id, status="cancelled",
                    error_message="Interrupted by user",
                    completed_at=shared.now_iso(),
                    duration_ms=int((time.monotonic() - start) * 1000),
                )
            else:
                # A cancellation the user never asked for (stall-reap,
                # shutdown, a cancelled pump task) is a platform failure —
                # never stamp it "cancelled", the runs page must be able to
                # tell the two apart.
                await asyncio.to_thread(
                    task_store.update_run, run_id, status="failed",
                    error_message=platform_reason or (
                        "Proxy shutting down" if shared._shutting_down
                        else "Interrupted by platform"
                    ),
                    completed_at=shared.now_iso(),
                    duration_ms=int((time.monotonic() - start) * 1000),
                )
            await interactive._close_interactive_task_session(session_id)
            if layer is not None:
                await layer.close_session(session_id)
            logger.info(
                f"Task cancelled: run={run_id} (user={user_stop}, "
                f"platform_reason={platform_reason or 'none'})"
            )
            # Delegate-only: tell the delegating agent the run was canceled, else its
            # delegate badge spins "running" forever and — getting no result — it may
            # re-delegate. Skipped during graceful shutdown (the delegating session is
            # being torn down too). _deliver_task_result self-gates on on_complete_agent,
            # so this no-ops for non-delegate tasks. A user-initiated Stop reports
            # "user_interrupted" with lane finalization (30s grace collects the
            # partial output + the user's redirect message, if any).
            if task.on_complete_agent and not shared._shutting_down and not result_delivered:
                if user_stop:
                    await delivery._deliver_task_result(
                        task, "user_interrupted",
                        f'⚠ Delegated task "{task.name}" was stopped by the user '
                        f'before it finished.',
                        worker_chat_id=chat_id, output_cursor=output_cursor,
                        prompt_row_id=prompt_row_id, prompt_text=prompt,
                    )
                else:
                    await delivery._deliver_task_result(
                        task, "cancelled",
                        f'⚠ Delegated task "{task.name}" was canceled before it finished.',
                    )

        except Exception as e:
            # A dead PTY on a watched interactive worker = the user closed or
            # killed the CLI deliberately — report user_interrupted, don't retry,
            # don't page anyone about a "failure".
            user_stop = isinstance(e, interactive._InteractiveSessionDied) and e.had_viewer
            logger.error(f"Task failed: run={run_id}, task={task.id}: {e}", exc_info=True)
            await asyncio.to_thread(
                task_store.update_run, run_id,
                status="cancelled" if user_stop else "failed",
                error_message="Interrupted by user" if user_stop else str(e),
                completed_at=shared.now_iso(),
                duration_ms=int((time.monotonic() - start) * 1000),
                chat_id=chat_id,
            )
            await interactive._close_interactive_task_session(session_id)
            if layer is not None:
                await layer.close_session(session_id)
            if (agent_cfg is not None and not session_started and not reused_warm
                    and not seat_released):
                # Built, never bound: close_session above found no binding to
                # release, so the seat would stay taken until a restart.
                from core.config.config_builder import release_config_seat
                release_config_seat(session_id, agent_cfg)

            # Collect whatever output was saved before the error; if none (a pre-turn
            # failure — bad config / dead process / offline target), deliver a
            # clearly-marked NON-EMPTY terminal so the delegating agent sees the
            # failure (not an empty bubble) and doesn't re-delegate. Delegate-only via
            # _deliver_task_result's on_complete gate; harmless for plain tasks.
            fail_output = lanes._collect_task_output(chat_id, output_cursor) or (
                f'⚠ Delegated task "{task.name}" was stopped by the user.'
                if user_stop else f'⚠ Delegated task "{task.name}" failed: {e}'
            )
            if result_delivered:
                logger.info(
                    f"Task {task.id}: result already delivered — skipping the "
                    f"failure re-delivery (post-delivery cleanup error)"
                )
            else:
                await delivery._deliver_task_result(
                    task, "user_interrupted" if user_stop else "failed", fail_output,
                    worker_chat_id=chat_id, output_cursor=output_cursor,
                    prompt_row_id=prompt_row_id, prompt_text=prompt,
                )

            # Failure safety net: fire warning notification for 'auto' and
            # 'manual' modes (a crashed agent can't notify itself). 'none' is
            # fully silent — user explicitly opted out of all notifications. A
            # deliberate user stop is not a failure — no page.
            if task.notification_mode != "none" and not user_stop:
                from services.notifications import notification_manager
                asyncio.create_task(notification_manager.fire_notification(
                    title=f"Task Failed: {task.name}",
                    body=str(e)[:200],
                    severity="warning",
                    scope=task.scope,
                    target=task.created_by if task.scope == "user" else task.agent,
                    source="task",
                    source_id=task.id,
                    agent_slug=task.agent,
                    chat_id=chat_id,
                ))

            if attempt < task.retry.max_attempts and not user_stop:
                logger.info(
                    f"Retrying task {task.id} "
                    f"(attempt {attempt + 1}/{task.retry.max_attempts})"
                )
                await asyncio.sleep(task.retry.delay_seconds)
                await _execute_task(
                    task, trigger_type=trigger_type,
                    trigger_source=trigger_source, attempt=attempt + 1,
                    trigger_payload=trigger_payload,
                )

        finally:
            shared._run_subscribers.pop(run_id, None)
            shared._run_event_buffer.pop(run_id, None)
            shared._user_cancelled_runs.discard(run_id)
            shared._platform_interrupts.pop(run_id, None)
            # Reap the task's in-memory session-index entry (added at the top of
            # _execute_task + step 4 of this function). Task sessions were never
            # removed, so _sessions leaked one is_task entry per run — growing
            # index.json unboundedly and (pre-fix) poisoning the recency lookups
            # that /v1/session/current + /v1/location/request used to do. The live
            # process is already closed by the layer, and any callback re-binds via
            # the persistent chat_id (not this entry), so dropping it here is safe.
            # Idempotent under retries (the retry's _run_task already popped it).
            # EXCEPT after a graceful abort: the session was deliberately left
            # warm, so its entry must survive for the idle reaper +
            # /v1/session/current until the layer actually closes it.
            if (not session_kept_warm
                    and _state._sessions.pop(session_id, None) is not None):
                _state._save_sessions()
            # Release the dedup reservation — but only if it still points at THIS
            # run, so a retry hand-off (which re-reserved task.id under its own
            # run_id) keeps its reservation until it finishes.
            if shared._active_task_ids.get(task.id) == run_id:
                shared._active_task_ids.pop(task.id, None)
            # Auto-cleanup fired one-time tasks that aren't trigger-only
            # tasks (those must persist to be re-fired by their wired-up
            # trigger). Recurring = cron schedule OR interval_seconds — both
            # must persist. Skip if retries remain — the retry creates a new
            # _run_task that needs the DB row.
            if (not task.schedule
                    and task.interval_seconds is None
                    and task.task_type != "trigger"
                    and attempt >= task.retry.max_attempts):
                try:
                    await asyncio.to_thread(task_store.delete_dynamic_task, task.id)
                    shared._dynamic_task_ids.discard(task.id)
                    logger.debug(f"Cleaned up fired one-time task: {task.id}")
                except Exception:
                    pass  # non-critical, task was already marked fired


async def _broadcast(run_id: str, event: dict) -> None:
    # Buffer event for late-subscriber replay (page refresh during active run)
    shared._run_event_buffer.setdefault(run_id, []).append(event)
    for q in list(shared._run_subscribers.get(run_id, [])):
        try:
            await q.put(event)
        except Exception as e:
            logger.debug("run event broadcast: %s", e)
