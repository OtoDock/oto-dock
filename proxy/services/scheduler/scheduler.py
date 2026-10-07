"""APScheduler-based task execution engine.

Loads agent-created dynamic tasks from task_store and registers them with
APScheduler. All tasks live in the DB — no filesystem definitions.

This module is the facade and the registry API (start, stop, shutdown, the
dynamic-task CRUD, cancel and subscribe, the timezone re-apply, the listing).
The engine lives in the pieces this module assembles:

* ``shared.py``      — TaskDefinition, RetryPolicy, the run registries, now_iso
* ``lanes.py``       — lane output, quiescence, settling, reuse, the stall watchdog
* ``interactive.py`` — interactive (PTY) task runs
* ``delivery.py``    — delegate result delivery and continuation wakes
* ``firing.py``      — spawn spacing and the self-continuation fire
* ``runner.py``      — _fire_task, _execute_task, _run_task (the frame) and
  _run_task_body (the admitted run), _end_run (the stamp, then the stream's done)

Consumers keep reading everything through this module (``from
services.scheduler import scheduler``); the names below are re-exported for
them and for the tests. A test that patches a private name patches the piece
that defines it (``lanes._await_lane_quiescence``), never this facade: the
pieces read their globals from their own module.
"""

import asyncio
import contextlib
import json
import logging
import zoneinfo
from datetime import datetime, timedelta, timezone

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.date import DateTrigger

import config
from services.scheduler import scheduler_triggers
from storage import database as task_store
from services.scheduler import runner, shared
from services.scheduler.shared import (  # noqa: F401
    RetryPolicy,
    TaskDefinition,
    _active_task_ids,
    _dynamic_task_ids,
    _platform_interrupts,
    _run_event_buffer,
    _run_subscribers,
    _running_tasks,
    _user_cancelled_runs,
    now_iso,
)
from services.scheduler.lanes import (  # noqa: F401
    _DELEGATE_PREVIEW_CAP,
    _TaskTurnStalled,
    _await_lane_quiescence,
    _collect_lane_output_since,
    _collect_task_output,
    _delegate_output_preview,
    _lane_pump_wedged,
    _limit_notice,
    _post_run_session_action,
    _reap_prior_lane_pump,
    _settle_prior_lane,
    _try_reuse_warm_session,
    _watch_task_pump,
)
from services.scheduler.interactive import (  # noqa: F401
    INTERACTIVE_TASK_MAX_S,
    _InteractiveSessionDied,
    _close_interactive_task_session,
    _run_interactive_task,
)
from services.scheduler.delivery import (  # noqa: F401
    _deliver_task_result,
    _deliver_via_oneshot,
    _deliver_via_persistent,
    _do_deliver,
    _rewarm_interactive_and_wake,
    _run_echo_turn_pumped,
    _wait_turn_open,
    redeliver_pending_wakes,
)
from services.scheduler.firing import (  # noqa: F401
    _SKIP_AGING_GRACE,
    _continuation_cursors,
    _continuation_skip_counts,
    _fire_continuation,
    _reserve_spawn_slot,
    _spawn_slot_lock,
    _spawn_spacing_gate,
)
from services.scheduler import task_kinds
from services.scheduler.runner import (  # noqa: F401
    _broadcast,
    _create_task_chat_row,
    _end_run,
    _end_unstamped,
    _execute_task,
    _fire_task,
    _is_valid_session_uuid,
    _run_task,
    _run_task_body,
)

logger = logging.getLogger("claude-proxy.scheduler")

_scheduler = AsyncIOScheduler(timezone=config.get_platform_timezone())
_current_tz: str = config.get_platform_timezone()  # tracks last-known timezone


def get_scheduler() -> AsyncIOScheduler:
    """Expose the scheduler instance for notification_manager."""
    return _scheduler


def start() -> None:
    """Load surviving dynamic tasks and start scheduler."""
    if config.SCHEDULER_MODE == "standalone":
        # Don't start APScheduler — an external standalone scheduler handles
        # scheduling. The proxy still serves CRUD APIs from the DB.
        logger.info("Scheduler in standalone mode — scheduling handled externally")
        return

    # Reload dynamic tasks that survived a proxy restart
    dynamic_rows = task_store.list_dynamic_tasks(enabled_only=True)
    reloaded = 0
    for row in dynamic_rows:
        if row.get("fired") and (row.get("run_at") or row.get("delay_seconds") is not None):
            continue  # one-time already fired, skip
        task = _row_to_task(row)
        shared._dynamic_task_ids.add(task.id)
        _register_task(task)
        reloaded += 1

    _scheduler.start()

    logger.info(f"Scheduler started: {reloaded} dynamic tasks reloaded")


def apply_platform_timezone_change() -> None:
    """Re-register recurring jobs after the platform timezone is changed.

    Called directly from the Setup API the moment ``platform_timezone`` is saved,
    so the new TZ takes effect immediately — event-driven, no polling job. No-op
    in standalone mode, where an external scheduler owns registration and
    ``get_scheduled_jobs()`` recomputes next-run times from task definitions live.
    """
    if config.SCHEDULER_MODE == "standalone":
        return
    _check_timezone_change()


def _check_timezone_change() -> None:
    """Re-register all recurring jobs if the platform timezone changed.

    Affects cron (CronTrigger) and interval (IntervalTrigger) tasks whose
    `user_tz` is NULL — those fall back to the platform TZ for their trigger
    timezone. One-time / delay tasks don't need re-registration.
    """
    global _current_tz
    new_tz = config.get_platform_timezone()
    if new_tz == _current_tz:
        return
    old_tz = _current_tz
    _current_tz = new_tz
    logger.info(f"Platform timezone changed: {old_tz} → {new_tz}, re-registering recurring jobs")

    # Re-register all dynamic recurring tasks
    for row in task_store.list_dynamic_tasks(enabled_only=True):
        task = _row_to_task(row)
        if task.schedule or task.interval_seconds is not None:
            _register_task(task)


def stop() -> None:
    if config.SCHEDULER_MODE == "standalone":
        return
    _scheduler.shutdown(wait=False)
    logger.info("Scheduler stopped")


async def shutdown() -> None:
    """Cancel all running task asyncio.Tasks for graceful proxy shutdown."""
    shared._shutting_down = True
    if not shared._running_tasks:
        logger.info("Scheduler shutdown: no running tasks")
        return

    count = len(shared._running_tasks)
    logger.info(f"Scheduler shutdown: cancelling {count} running task(s)")
    for run_id, task in list(shared._running_tasks.items()):
        if not task.done():
            task.cancel()

    # Wait for tasks to acknowledge cancellation
    tasks = [t for t in shared._running_tasks.values() if not t.done()]
    if tasks:
        done, pending = await asyncio.wait(tasks, timeout=10)
        if pending:
            logger.warning(
                f"Scheduler shutdown: {len(pending)} task(s) didn't finish in 10s"
            )

    logger.info("Scheduler shutdown: all running tasks cancelled")


def _resolve_task_tz(task: shared.TaskDefinition):
    """Pick the IANA tz to interpret naive timestamps and drive cron triggers.

    Order: row's user_tz (browser-snapshotted at create) → platform TZ.
    Invalid IANA names fall back to platform TZ silently.
    """
    name = task.user_tz or config.get_platform_timezone()
    try:
        return zoneinfo.ZoneInfo(name)
    except Exception:
        logger.warning(f"Invalid user_tz {name!r} on task {task.id}; falling back to platform TZ")
        return zoneinfo.ZoneInfo(config.get_platform_timezone())


def _register_task(task: shared.TaskDefinition) -> None:
    """Register task with APScheduler."""
    job_id = f"task_{task.id}"
    task_tz = _resolve_task_tz(task)

    # Trigger-only tasks don't get an APScheduler entry — they're fired by
    # the trigger fire path (services/scheduler/trigger_manager.py). Storing them
    # without an APScheduler job is the correct end state.
    if not task_kinds.of(task).clocked:
        logger.debug(f"Skipping APScheduler registration for trigger-only task: {task.id}")
        return

    if task.run_at:
        try:
            run_date = datetime.fromisoformat(task.run_at)
            if run_date.tzinfo is None:
                # Naive ISO → interpret in the row's TZ (user_tz or platform fallback).
                # Pre-fix this assumed UTC, which clashed with notification_manager's
                # platform-TZ assumption. Naive-as-row-TZ is consistent with the
                # MCP tool description that tells agents to write local time.
                run_date = run_date.replace(tzinfo=task_tz)
            if run_date < datetime.now(timezone.utc):
                logger.debug(f"Skipping past one-time task: {task.id}")
                return
            trigger = DateTrigger(run_date=run_date)
        except Exception as e:
            logger.warning(f"Invalid run_at for task {task.id}: {e}")
            return
    elif task.delay_seconds is not None:
        run_date = datetime.now(timezone.utc) + timedelta(seconds=task.delay_seconds)
        trigger = DateTrigger(run_date=run_date)
    elif task.interval_seconds is not None:
        # Anchor start_date at `created_at + interval_seconds` so first fire is
        # exactly one interval after creation and the cadence is deterministic
        # across restarts/edits (see scheduler_triggers.build_interval_trigger).
        try:
            trigger = scheduler_triggers.build_interval_trigger(
                task.interval_seconds, task.created_at, task_tz,
            )
        except Exception as e:
            logger.warning(f"Invalid interval_seconds for task {task.id}: {e}")
            return
    elif task.schedule:
        try:
            trigger = scheduler_triggers.build_cron_trigger(task.schedule, task_tz)
        except Exception as e:
            logger.warning(f"Invalid cron schedule for task {task.id}: {e}")
            return
    else:
        logger.warning(f"Task {task.id} has no schedule/run_at/delay_seconds/interval_seconds — skipping")
        return

    _scheduler.add_job(
        runner._fire_task,
        trigger=trigger,
        id=job_id,
        args=[task],
        replace_existing=True,
        misfire_grace_time=300,
        coalesce=True,
    )
    logger.info(f"Registered task: {task.id} (agent={task.agent})")


def _row_to_task(row: dict) -> shared.TaskDefinition:
    """Convert a dynamic_tasks DB row to a shared.TaskDefinition."""
    created_at = row.get("created_at")
    if created_at is not None and not isinstance(created_at, str):
        # psycopg may return datetime; normalise to ISO string for the model
        created_at = created_at.isoformat()
    return shared.TaskDefinition(
        id=row["id"],
        name=row["name"],
        agent=row["agent"],
        llm_mode=_col(row, "llm_mode", "cli"),
        prompt=row["prompt"],
        schedule=row.get("schedule") or "",
        run_at=row.get("run_at"),
        delay_seconds=row.get("delay_seconds"),
        interval_seconds=row.get("interval_seconds"),
        timeout_seconds=_col(row, "timeout_seconds", 600),
        enabled=bool(row.get("enabled", 1)),
        created_by=row.get("created_by"),
        created_at=created_at,
        on_complete_agent=row.get("on_complete_agent"),
        on_complete_prompt=row.get("on_complete_prompt"),
        on_complete_session_id=row.get("on_complete_session_id"),
        continue_session=row.get("continue_session"),
        use_persistent=bool(row.get("use_persistent", 0)),
        notification_mode=row.get("notification_mode") or "manual",
        notify_severity=_col(row, "notify_severity", "info"),
        scope=_col(row, "scope", "user"),
        user_tz=row.get("user_tz"),
        task_type=row.get("task_type", ""),
        target_chat_id=row.get("target_chat_id"),
        max_runs=row.get("max_runs"),
        until_at=row.get("until_at"),
        # "" (the column default) means inherit — normalise to None so every
        # `override or default` consumer reads the same way.
        override_model=row.get("override_model") or None,
        override_execution_path=row.get("override_execution_path") or None,
        app_id=row.get("app_id") or None,
        app_handler=row.get("app_handler") or None,
        checks=_checks_of(row.get("checks")),
        transferred_from=row.get("transferred_from") or "",
        transferred_at=row.get("transferred_at") or "",
    )


def _col(row: dict, key: str, default):
    """A column a TaskDefinition field requires, ``default`` when NULL:
    ``row.get(key, default)`` reads a NULL as None, which the model refuses
    — one such row failed every task listing and the scheduler's start."""
    val = row.get(key)
    return default if val is None else val


def _checks_of(raw) -> list[str]:
    """The ``checks`` column (a JSON list of refs, '' = none) as a list."""
    if not raw:
        return []
    try:
        val = json.loads(raw) if isinstance(raw, str) else raw
    except ValueError:
        return []
    return [r for r in val if isinstance(r, str)] if isinstance(val, list) else []


def get_scheduled_jobs() -> list[dict]:
    """Return APScheduler jobs for user/agent dynamic tasks with next run times.

    Only jobs whose id is prefixed ``task_`` (real dynamic tasks) are reported.
    Internal housekeeping jobs — e.g. ``_tz_sync`` (the per-minute timezone
    watcher) — are scheduler implementation details and must never surface in
    the dashboard's schedules list or scheduled-task counts.
    """
    if config.SCHEDULER_MODE == "standalone":
        return _compute_next_run_times()
    jobs = []
    dyn_rows = {r["id"]: r for r in task_store.list_dynamic_tasks(enabled_only=False)}
    for job in _scheduler.get_jobs():
        if not job.id.startswith("task_"):
            continue  # internal housekeeping job (e.g. _tz_sync), not a task
        task_id = job.id.removeprefix("task_")
        row = dyn_rows.get(task_id)
        agent = row["agent"] if row else "unknown"
        name = row["name"] if row else task_id
        # `next_run_time` is unset on a job that exists but isn't scheduled yet
        # (scheduler not started, or paused) — getattr keeps this resilient.
        next_run = getattr(job, "next_run_time", None)
        jobs.append({
            "id": job.id,
            "task_id": task_id,
            "name": name,
            "agent": agent,
            "next_run_time": next_run.isoformat() if next_run else None,
        })
    return jobs


def next_run_times() -> dict[str, str | None]:
    """Every scheduled task's next fire as ISO, keyed by task id, built
    ONCE: the in-memory jobs in embedded mode (no DB), one computation over
    the definitions in standalone mode. The listing reads this instead of
    ``get_scheduled_jobs`` (a second full read of the table) or one
    ``next_run_time`` per task (a full computation each, standalone)."""
    if config.SCHEDULER_MODE == "standalone":
        return {j["task_id"]: j["next_run_time"] for j in _compute_next_run_times()}
    out: dict[str, str | None] = {}
    for job in _scheduler.get_jobs():
        if not job.id.startswith("task_"):
            continue
        next_run = getattr(job, "next_run_time", None)
        out[job.id.removeprefix("task_")] = next_run.isoformat() if next_run else None
    return out


def next_run_time(task_id: str) -> str | None:
    """One task's next fire as ISO, None when no job is scheduled for it.

    Embedded mode reads the job straight from APScheduler (no DB, so it is
    safe from the writer thread that finishes a run and from the event
    loop); standalone mode computes it from the definitions like
    ``get_scheduled_jobs`` does.
    """
    if config.SCHEDULER_MODE == "standalone":
        for job in _compute_next_run_times():
            if job["task_id"] == task_id:
                return job["next_run_time"]
        return None
    job = _scheduler.get_job(f"task_{task_id}")
    next_run = getattr(job, "next_run_time", None) if job else None
    return next_run.isoformat() if next_run else None


def _compute_next_run_times() -> list[dict]:
    """Compute next run times from task definitions (standalone mode).

    Uses CronTrigger / IntervalTrigger to compute when each recurring task
    would next fire, without needing a running APScheduler instance.
    """
    jobs = []
    all_tasks = [_row_to_task(row) for row in task_store.list_dynamic_tasks(enabled_only=True)]

    for task in all_tasks:
        if not task.enabled:
            continue
        task_tz = _resolve_task_tz(task)
        next_run = None
        has_recurring = False
        if task.schedule:
            has_recurring = True
            try:
                trigger = scheduler_triggers.build_cron_trigger(task.schedule, task_tz)
                next_fire = trigger.get_next_fire_time(
                    None, datetime.now(timezone.utc)
                )
                next_run = next_fire.isoformat() if next_fire else None
            except Exception:
                pass
        elif task.interval_seconds is not None:
            has_recurring = True
            try:
                trigger = scheduler_triggers.build_interval_trigger(
                    task.interval_seconds, task.created_at, task_tz,
                )
                next_fire = trigger.get_next_fire_time(
                    None, datetime.now(timezone.utc)
                )
                next_run = next_fire.isoformat() if next_fire else None
            except Exception:
                pass
        elif task.run_at:
            next_run = task.run_at
        # Skip delay_seconds tasks (they're relative, already computed at creation)

        if next_run or has_recurring:
            jobs.append({
                "id": f"task_{task.id}",
                "task_id": task.id,
                "name": task.name,
                "agent": task.agent,
                "next_run_time": next_run,
            })
    return jobs


def get_running_tasks() -> list[dict]:
    """Return currently executing run IDs."""
    return [{"run_id": rid} for rid in shared._running_tasks]


def get_all_task_definitions() -> list[shared.TaskDefinition]:
    """Return all dynamic tasks."""
    return [_row_to_task(row) for row in task_store.list_dynamic_tasks(enabled_only=False)]


async def trigger_task_now(task: shared.TaskDefinition, trigger_type: str = "manual",
                           trigger_source: str | None = None,
                           prompt_override: str | None = None,
                           trigger_payload: dict | None = None) -> str:
    """Immediately execute a task. Returns run_id.

    ``trigger_payload``: set by ``trigger_manager`` when this
    task was fired by a webhook trigger. Carries the normalised event data
    that downstream ``agent_context`` builder blocks resolve via
    ``${trigger.*}`` tokens. ``None`` for manual / scheduled fires.
    """
    return await runner._execute_task(task, trigger_type=trigger_type,
                               trigger_source=trigger_source,
                               prompt_override=prompt_override,
                               trigger_payload=trigger_payload)


async def add_dynamic_task(task: shared.TaskDefinition) -> str:
    """Persist to DB + register with APScheduler live. Returns task.id."""
    # Resolve task_type: explicit value wins; otherwise auto-derive.
    # Recurring = cron schedule OR interval_seconds. One-time = run_at/delay.
    task_type = task.task_type or task_kinds.derive(task.schedule, task.interval_seconds)
    await asyncio.to_thread(
        task_store.create_dynamic_task,
        task.id, task.agent, task.name, task.prompt, task.llm_mode,
        task_type,
        task.schedule or None,
        task.run_at,
        task.delay_seconds,
        task.timeout_seconds,
        task.created_by,
        # Keyword args from here: create_dynamic_task has on_complete_chat_id
        # between on_complete_session_id and continue_session — positional
        # passing shifted continue_session/use_persistent one column left
        # (bool-as-string rows in continue_session; use_persistent never
        # persisted, so delegate rows lost their classification on reload).
        on_complete_agent=task.on_complete_agent,
        on_complete_prompt=task.on_complete_prompt,
        on_complete_session_id=task.on_complete_session_id,
        continue_session=task.continue_session,
        use_persistent=task.use_persistent,
        scope=task.scope,
        notification_mode=task.notification_mode,
        notify_severity=task.notify_severity,
        user_tz=task.user_tz,
        interval_seconds=task.interval_seconds,
        target_chat_id=task.target_chat_id,
        max_runs=task.max_runs,
        until_at=task.until_at,
        override_model=task.override_model,
        override_execution_path=task.override_execution_path,
        app_id=task.app_id,
        app_handler=task.app_handler,
        checks=json.dumps(list(task.checks)) if task.checks else "",
    )
    shared._dynamic_task_ids.add(task.id)
    # Hydrate the in-memory task with the DB-side timestamp so _register_task
    # can anchor IntervalTrigger.start_date correctly (otherwise it falls back
    # to "now", which would drift from the DB row on standalone-mode pickup).
    if task.created_at is None:
        refreshed = await asyncio.to_thread(task_store.get_dynamic_task, task.id)
        if refreshed:
            ca = refreshed.get("created_at")
            if ca is not None and not isinstance(ca, str):
                ca = ca.isoformat()
            task.created_at = ca
    # Trigger-only tasks: skip APScheduler registration (no schedule, no run_at).
    # They fire only via the trigger system.
    if config.SCHEDULER_MODE != "standalone" and (task_kinds.of_word(task_type) or task_kinds.of(task)).clocked:
        # Update the in-memory task with the resolved task_type so _register_task
        # sees it (rare path: TaskDefinition created without task_type set).
        if not task.task_type:
            task.task_type = task_type
        _register_task(task)
    return task.id


async def remove_dynamic_task(task_id: str) -> bool:
    """Remove from DB + unschedule from APScheduler."""
    deleted = await asyncio.to_thread(task_store.delete_dynamic_task, task_id)
    shared._dynamic_task_ids.discard(task_id)
    if config.SCHEDULER_MODE != "standalone":
        job_id = f"task_{task_id}"
        job = _scheduler.get_job(job_id)
        if job:
            _scheduler.remove_job(job_id)
    return deleted


async def pause_dynamic_task(task_id: str) -> bool:
    """Set enabled=FALSE in DB and remove APScheduler job (embedded mode).

    Returns True if the dynamic task exists (regardless of prior state).
    Idempotent — safe to call repeatedly. Standalone mode picks up the change
    on the next periodic sync (≤ SCHEDULER_SYNC_INTERVAL).
    """
    dyn = await asyncio.to_thread(task_store.get_dynamic_task, task_id)
    if not dyn:
        return False
    await asyncio.to_thread(task_store.set_dynamic_task_enabled, task_id, False)
    if config.SCHEDULER_MODE != "standalone":
        job_id = f"task_{task_id}"
        # job may not exist (e.g. past run_at, already-fired)
        with contextlib.suppress(Exception):
            _scheduler.remove_job(job_id)
    return True


async def resume_dynamic_task(task_id: str) -> bool:
    """Set enabled=TRUE in DB and re-register APScheduler job (embedded mode).

    Returns True if the dynamic task exists. For one-time tasks whose
    ``run_at`` is in the past, ``_register_task`` returns early — the row
    stays enabled but no job is scheduled. The user can fire manually via the
    Run button. Standalone mode picks up the change on the next periodic
    sync (≤ SCHEDULER_SYNC_INTERVAL).
    """
    dyn = await asyncio.to_thread(task_store.get_dynamic_task, task_id)
    if not dyn:
        return False
    await asyncio.to_thread(task_store.set_dynamic_task_enabled, task_id, True)
    if config.SCHEDULER_MODE != "standalone":
        # Re-fetch the (now-enabled) row before registering so we have fresh state.
        refreshed = await asyncio.to_thread(task_store.get_dynamic_task, task_id)
        if refreshed and refreshed.get("enabled"):
            task = _row_to_task(refreshed)
            _register_task(task)
    return True


# Validation: timing fields are mutually exclusive. The service helper
# normalises the edit payload — if the caller sets a new schedule we clear
# run_at + interval_seconds, and so on.
_TIMING_FIELDS = {"schedule", "run_at", "interval_seconds", "user_tz"}

# Interval bounds match the docs / MCP / API description. Min 60s (cron's
# 1-minute granularity); max 1 year. Longer cadences should use cron.
INTERVAL_MIN_SECONDS = 60
INTERVAL_MAX_SECONDS = 365 * 24 * 60 * 60  # 31_536_000


def _validate_interval_seconds(value) -> str | None:
    """Return None if valid, otherwise a human-readable error string."""
    if not isinstance(value, int) or isinstance(value, bool):
        return "interval_seconds must be an integer"
    if value < INTERVAL_MIN_SECONDS or value > INTERVAL_MAX_SECONDS:
        return (
            f"interval_seconds must be between {INTERVAL_MIN_SECONDS} "
            f"and {INTERVAL_MAX_SECONDS}"
        )
    return None


async def update_dynamic_task(task_id: str, fields: dict) -> tuple[bool, str | None]:
    """Apply a partial update + reschedule the APScheduler job if timing changed.

    Validates cron / ISO datetime, normalises mutually exclusive timing
    fields (setting one clears the other), updates DB, then in embedded
    mode replaces the existing APScheduler job with a fresh registration.
    Standalone mode picks up the change on the next periodic sync.

    Returns ``(ok, error_message)``. ``error_message`` is non-empty when the
    request was rejected for validation reasons (caller maps to HTTP 400).
    """
    dyn = await asyncio.to_thread(task_store.get_dynamic_task, task_id)
    if not dyn:
        return False, None  # caller maps to 404
    # An app handler's row is kept in step with its manifest at every
    # go-live (APPS.md "Handlers"); an edit here would turn it back into a
    # prompt task on its next fire. Pause and resume stay open.
    if dyn.get("app_id"):
        return False, ("this schedule comes from the app's manifest — change app.json "
                       "and redeploy")

    payload = dict(fields)  # don't mutate caller's dict

    # Validate user_tz first — it drives naive run_at parsing below.
    edit_tz_name: str | None = None
    if "user_tz" in payload and payload["user_tz"] is not None:
        try:
            zoneinfo.ZoneInfo(payload["user_tz"])
        except Exception as e:
            return False, f"Invalid user_tz: {e}"
        edit_tz_name = payload["user_tz"]
    else:
        edit_tz_name = dyn.get("user_tz") or config.get_platform_timezone()
    edit_tz = zoneinfo.ZoneInfo(edit_tz_name) if edit_tz_name else zoneinfo.ZoneInfo(config.get_platform_timezone())

    # Validate cron string against the post-edit TZ (build_cron_trigger, so
    # the standard-cron day-of-week remap validates too — never from_crontab)
    if "schedule" in payload and payload["schedule"] is not None:
        try:
            scheduler_triggers.build_cron_trigger(payload["schedule"], edit_tz)
        except Exception as e:
            return False, f"Invalid cron schedule: {e}"

    # Validate ISO datetime — naive interpreted in post-edit TZ (NOT UTC).
    # Aligns with notification_manager and the new browser-snapshot model.
    if "run_at" in payload and payload["run_at"] is not None:
        try:
            run_date = datetime.fromisoformat(payload["run_at"])
            if run_date.tzinfo is None:
                payload["run_at"] = run_date.replace(tzinfo=edit_tz).isoformat()
        except Exception as e:
            return False, f"Invalid run_at: {e}"

    # Validate interval bounds when present
    if "interval_seconds" in payload and payload["interval_seconds"] is not None:
        err = _validate_interval_seconds(payload["interval_seconds"])
        if err:
            return False, err

    # Mutual exclusivity: setting one timing field clears the others and
    # auto-derives task_type. Caller never has to set the cleared fields manually.
    if payload.get("schedule"):
        payload["interval_seconds"] = None
        payload["run_at"] = None
        payload["task_type"] = task_kinds.SCHEDULED
    elif payload.get("interval_seconds"):
        payload["schedule"] = None
        payload["run_at"] = None
        payload["task_type"] = task_kinds.SCHEDULED
    elif payload.get("run_at"):
        payload["schedule"] = None
        payload["interval_seconds"] = None
        payload["task_type"] = task_kinds.ONE_TIME

    timing_changed = any(k in payload for k in _TIMING_FIELDS)

    ok = await asyncio.to_thread(task_store.update_dynamic_task, task_id, payload)
    if not ok:
        return False, None

    # Re-register only when timing changed and we're in embedded mode. APScheduler's
    # add_job replace_existing=True swaps the trigger atomically; if the task is
    # currently disabled, we leave the job slot empty (resume will register).
    if timing_changed and config.SCHEDULER_MODE != "standalone":
        refreshed = await asyncio.to_thread(task_store.get_dynamic_task, task_id)
        if refreshed and refreshed.get("enabled"):
            # Drop any existing job before re-registering so a switch from
            # recurring → one-time-past-run_at correctly leaves no job.
            with contextlib.suppress(Exception):
                _scheduler.remove_job(f"task_{task_id}")
            task = _row_to_task(refreshed)
            _register_task(task)
    return True, None


async def cancel_run(run_id: str) -> bool:
    """Cancel a running asyncio.Task. Callers of this function are user
    surfaces (run-cancel API / dashboard Stop), so the run is stamped
    user-cancelled — its delegate callback reports "user_interrupted"."""
    t = shared._running_tasks.get(run_id)
    if t and not t.done():
        shared._user_cancelled_runs.add(run_id)
        t.cancel()
        return True
    return False


def platform_cancel_run(run_id: str, reason: str) -> bool:
    """Cancel a run the PLATFORM decided to stop (stall-reap, unusable
    session) — never a user action. The run is stamped failed with
    ``reason`` so the runs page distinguishes it from a user cancel.

    Returns True when a live scheduler task was cancelled. False means no
    scheduler task drives this run (e.g. a dashboard-driven continuation
    turn on a task chat) — the caller stamps the row directly."""
    t = shared._running_tasks.get(run_id)
    if t and not t.done():
        shared._platform_interrupts[run_id] = reason
        t.cancel()
        return True
    return False


async def subscribe_run(run_id: str) -> asyncio.Queue:
    q: asyncio.Queue = asyncio.Queue()
    # Replay buffered events so late subscribers (e.g. after a page refresh) catch up
    for event in shared._run_event_buffer.get(run_id, []):
        await q.put(event)
    shared._run_subscribers.setdefault(run_id, []).append(q)
    return q


def unsubscribe_run(run_id: str, q: asyncio.Queue) -> None:
    subs = shared._run_subscribers.get(run_id, [])
    if q in subs:
        subs.remove(q)
    if not subs:
        shared._run_subscribers.pop(run_id, None)
