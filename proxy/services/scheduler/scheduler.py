"""APScheduler-based task execution engine.

Loads agent-created dynamic tasks from task_store and registers them with
APScheduler. All tasks live in the DB — no filesystem definitions.
"""

import asyncio
import contextlib
import json
import logging
import time
import uuid
import zoneinfo
from datetime import datetime, timedelta, timezone

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.date import DateTrigger
from pydantic import BaseModel

import config
from services.scheduler import scheduler_triggers
from storage import agent_store
from storage import database as task_store

logger = logging.getLogger("claude-proxy.scheduler")

_scheduler = AsyncIOScheduler(timezone=config.get_platform_timezone())
_running_tasks: dict[str, asyncio.Task] = {}
_run_subscribers: dict[str, list[asyncio.Queue]] = {}
_run_event_buffer: dict[str, list[dict]] = {}  # run_id → all events for replay
_dynamic_task_ids: set[str] = set()
# task_id → run_id of the currently-active run for that task. General task
# robustness: one active run per task at a time, so a scheduled+manual collision
# or a standalone-scheduler retry can't double-fire. Reserved in _execute_task
# (first attempt only) and released in _run_task's finally. (Distinct delegate
# task_ids are NOT blocked — that re-delegation driver is fixed by reliable
# terminal delivery, not here.)
_active_task_ids: dict[str, str] = {}
# Set during graceful shutdown so the per-run cancel handler skips delegate
# result-delivery (the delegating session is being torn down too).
_shutting_down: bool = False
# run_ids cancelled BY A USER via cancel_run (dashboard Stop / cancel API) —
# distinguishes a deliberate user stop (delegates report "user_interrupted")
# from a shutdown/system cancel. Discarded in _run_task's finally.
_user_cancelled_runs: set[str] = set()
# run_id → reason for a PLATFORM-initiated interrupt (stall-reap, shutdown).
# Read by the per-run CancelledError handler so the runs page can tell a
# platform reap (failed + reason) from a user cancel (cancelled). Popped in
# _run_task's finally (run_ids are never reused — must not accumulate).
_platform_interrupts: dict[str, str] = {}
# Continuation coalescing: chat_id → the chat's last message id right after a
# wake was delivered. A new wake is SKIPPED while that id hasn't advanced —
# the previous wake is still unprocessed in the chat (queued behind a turn /
# gate), and stacking wake prompts would burn turns on stale pings. In-memory
# only: a restart forgets the cursor, worst case one extra wake (documented).
_continuation_cursors: dict[str, int] = {}
# Consecutive coalesce-skips per continuation task id. A recurring continuation
# bounded only by max_runs on a permanently-dead chat never increments
# run_count (skips return early), so the row would outlive its bound until
# manual delete. From the 3rd consecutive skip onward each skip counts toward
# max_runs; the first two stay free so a slow live turn never burns runs.
# In-memory like the cursors — a restart just restarts the grace window.
_continuation_skip_counts: dict[str, int] = {}
_SKIP_AGING_GRACE = 2
_current_tz: str = config.get_platform_timezone()  # tracks last-known timezone


def _collect_task_output(chat_id: str, after_id: int = 0) -> str:
    """Collect task output from chat_messages for delivery/storage.

    ``after_id`` is the pre-run message cursor: only rows persisted after it
    are collected, so a run on a chat that already has history (a target-chat
    worker, a multi-round continue) reports THIS round's output instead of
    re-sending every prior round."""
    messages = task_store.get_chat_messages(chat_id)
    parts = []
    for m in messages:
        if after_id and (m.get("id") or 0) <= after_id:
            continue
        if m["role"] == "assistant" and m.get("content"):
            parts.append(m["content"])
    return "\n\n".join(parts)


def _limit_notice(final_output: str) -> str | None:
    """Detect a provider usage-limit notice ending a run's output.

    The CLIs stream the limit notice as NORMAL result text ("You've hit your
    session limit · resets 3pm", "Claude AI usage limit reached|<ts>"), so the
    turn ends cleanly and the run would be stamped `completed` with the notice
    as its output. Only the LAST non-empty line is eligible and it must START
    with a known notice shape — a run whose real output merely discusses usage
    limits doesn't match.
    """
    if not final_output:
        return None
    tail = final_output[-300:].replace("’", "'")
    for line in reversed(tail.splitlines()):
        line = line.strip()
        if not line:
            continue
        low = line.lower()
        if ((low.startswith("you've hit your") and "limit" in low)
                or low.startswith("claude ai usage limit reached")
                or low.startswith("usage limit reached")):
            return line
        return None
    return None


def _collect_lane_output_since(chat_id: str, after_id: int = 0,
                               skip_row_id: int = 0, own_prompt: str = "") -> str:
    """Batched delegate-lane collection: everything the lane produced after
    the pre-run cursor — assistant turns verbatim, USER rows labelled
    ``[User interjected]`` so the delegating agent sees redirects/steering in
    order. The run's own driven prompt is excluded: by row id when the caller
    persisted it (headless), by first-content-match otherwise (interactive
    runs, where the transcript tailer backfills it as a user row)."""
    own_prompt = (own_prompt or "").strip()
    own_prompt_pending = bool(own_prompt)
    messages = task_store.get_chat_messages(chat_id)
    parts = []
    for m in messages:
        mid = m.get("id") or 0
        if after_id and mid <= after_id:
            continue
        if skip_row_id and mid == skip_row_id:
            continue
        content = m.get("content") or ""
        if m["role"] == "assistant" and content:
            parts.append(content)
        elif m["role"] == "user" and content:
            if own_prompt_pending and content.strip() == own_prompt:
                own_prompt_pending = False
                continue
            parts.append(f"[User interjected]: {content}")
    return "\n\n".join(parts)


async def _await_lane_quiescence(chat_id: str, *, ceiling_seconds: float = 1800.0,
                                 settle_seconds: float = 5.0,
                                 immediate_quiet_ok: bool = True) -> None:
    """Wait until no live activity remains on a delegate lane's chat.

    A user may be steering the worker when its driven turn ends — a queued
    pump message, a follow-up turn, or an open interactive turn. Collecting
    mid-activity would deliver a half-conversation, so wait for the lane to
    stay quiet for ``settle_seconds`` (first probe quiet → return at once —
    the common fire-and-forget case pays no latency). ``ceiling_seconds``
    bounds the wait (heartbeat-logged); runs INSIDE the delivery task, never
    under the run's RAM slot.

    ``immediate_quiet_ok=False`` disables the first-probe fast path: an
    interrupted lane is quiet BECAUSE the user just stopped it — their
    redirect (and the worker's reply to it) is what the settle window is
    there to capture."""
    from core.events.stream_pump import _active_pumps
    from core.session import interactive_session

    start = time.monotonic()
    last_heartbeat = start
    quiet_since: float | None = None
    while True:
        pump = _active_pumps.get(chat_id)
        busy = pump is not None and (not pump.is_done or bool(pump.message_queue))
        if not busy:
            live = interactive_session.find_live_for_chat(chat_id)
            busy = live is not None and (live._turn_open or bool(live._prompt_queue))
        now = time.monotonic()
        if busy:
            quiet_since = None
        elif quiet_since is None:
            if immediate_quiet_ok and now - start < 1.0:
                return  # quiet on first probe — nothing ever queued
            quiet_since = now
        elif now - quiet_since >= settle_seconds:
            return
        if now - start >= ceiling_seconds:
            logger.warning(
                f"Lane quiescence ceiling reached for chat {chat_id[:8]} "
                f"({int(ceiling_seconds)}s) — collecting anyway"
            )
            return
        if now - last_heartbeat >= 60.0:
            logger.info(f"Delegate delivery waiting on active lane chat {chat_id[:8]}")
            last_heartbeat = now
        await asyncio.sleep(1.0)


def _lane_pump_wedged(pump) -> bool:
    """Can the prior round's open pump still make progress? Healthy live
    pumps get a quiescence wait instead of a reap — a reap here would cancel
    a user's in-flight turn on the lane (its producer holds
    layer.session_lock; the reap shoots the holder)."""
    sid = pump.session_id
    from core.session.session_manager import _remote_layer
    if _remote_layer is not None and sid in _remote_layer._sessions:
        info = _remote_layer._sessions[sid]
        return _remote_layer.remote_stream_severed(sid) or info.cli_dead
    from core.layers.cli.session import _persistent_sessions
    ps = _persistent_sessions.get(sid)
    if ps is not None:
        return ps.proc is None or ps.proc.returncode is not None
    from core.layers.codex.session import _codex_sessions
    from core.layers.direct.session import _direct_sessions
    if sid in _codex_sessions or sid in _direct_sessions:
        return False
    # No live session backs the pump at all — orphaned producer.
    return True


async def _settle_prior_lane(chat_id: str, run_id: str) -> bool:
    """Run-start lane gate: wait out a HEALTHY prior round / user turn (the
    delivery-side quiescence gate never covered run START — the reap used to
    cancel live turns), then reap only what is genuinely wedged. Returns True
    when a reap happened — the round must then respawn, never reuse: a
    producer-cancel leaves the satellite's per-turn read loop running, so
    reusing would put two concurrent stdout readers on one CLI, while the
    respawn path's stale-live guard closes the old process first."""
    from core.events.stream_pump import _active_pumps
    prior = _active_pumps.get(chat_id)
    if prior is None or prior.is_done:
        return False
    if not _lane_pump_wedged(prior):
        await _await_lane_quiescence(chat_id)
        prior = _active_pumps.get(chat_id)
        if prior is None or prior.is_done:
            return False
    await _reap_prior_lane_pump(chat_id, run_id)
    return True


async def _try_reuse_warm_session(
    layer, agent_cfg, session_id: str, run_id: str,
) -> bool:
    """A2 reuse decision for a delegate continue round: True = drive the turn
    on the already-alive warm REMOTE session instead of respawning.

    Remote-only: local start_session already reuses an alive process and
    re-stamps identity/binding internally, but the remote spawn path commands
    the satellite, whose stale-live guard KILLS the existing CLI (the
    2026-07-20 incident destroyed a healthy worker mid-round this way).
    Every fall-through lands on the respawn path — the correct cleanup for
    dead/reaped/foreign states. Conditions:
    - the resolved layer is the remote layer, it holds the session alive,
      no turn in flight, process not known-dead;
    - the session lives on the machine this run resolved to;
    - the session was task-spawned (is_task): a dashboard-warmed session on
      the lane chat runs under the viewer's context/subscription/env — a
      task round must not ride it."""
    from core.session import session_state as _state
    from core.session.session_manager import _remote_layer
    if _remote_layer is None or layer is not _remote_layer:
        return False
    info = _remote_layer._sessions.get(session_id)
    if info is None or not info.alive or info.cli_dead or info.turn_active:
        return False
    if info.machine_id != (agent_cfg.execution_target or ""):
        return False
    if not (_state._sessions.get(session_id) or {}).get("is_task"):
        return False
    if await _remote_layer.is_session_process_dead(session_id):
        return False
    logger.info(
        f"Task {run_id}: reusing warm remote session {session_id[:8]} "
        f"on {info.machine_id[:8]} (no respawn)"
    )
    return True


def _post_run_session_action(chat_row: dict | None) -> tuple[str, bool]:
    """Map a finished lane turn's abort flags to (final_status, keep_warm).

    A user-stopped lane's session close depends on HOW it stopped. GRACEFUL
    abort (soft interrupt landed, turn closed normally): the CLI/daemon +
    MCP sidecars survived by contract — keep the session warm exactly like a
    dashboard chat, so the next prompt or continue round reuses it (no cold
    re-warm, live model changes apply, still-running subagent children keep
    reporting); the idle reaper collects it if nobody returns. HARD abort:
    the process is gone / the stream is not reliably writable again — close
    outright: the chat row keeps its session_id, so the user's next message
    takes the lazy-warmup path (honoring a model change made while stopped)
    and --resume restores the conversation. Interactive-PTY stops and run
    crashes never stamp the graceful flag, so they always close."""
    row = chat_row or {}
    if not row.get("last_turn_aborted"):
        return "completed", False
    return "user_interrupted", bool(row.get("last_abort_graceful"))


async def _reap_prior_lane_pump(chat_id: str, run_id: str) -> None:
    """Persist-and-clear a PRIOR round's pump before a new round fires on a
    continued lane (delegate ``continue_id`` / any run on ``target_chat_id``).

    A previous turn's pump can still be open here — typically a wedged one
    whose turn-end never arrived (severed satellite event stream). Registering
    the new round's pump would orphan it SILENTLY with its unflushed turn
    blocks, erasing that round's transcript from chat_messages (the dashboard
    then shows only the new round). Abort it and await its teardown — the
    pump's ``finally`` persists the partial turn — so the prior transcript is
    durably in the DB before this round's output cursor and prompt land."""
    from core.events.stream_pump import _active_pumps
    prior = _active_pumps.get(chat_id)
    if prior is None or prior.is_done:
        return
    logger.warning(
        f"Task {run_id}: prior round's pump still open on lane chat "
        f"{chat_id[:8]} (session={prior.session_id[:8]}) — reaping so its "
        f"turn persists before the new round"
    )
    prior.abort()
    if prior._task is not None:
        # Wait WITHOUT cancelling — the pump's finally flushes the partial
        # turn; a laggard past the timeout is left to finish on its own.
        await asyncio.wait([prior._task], timeout=5.0)


# Hard max-time backstop for an interactive task run:
# if the turn-end signal never lands (CLI hung / crashed), the run is failed with
# a clear timeout rather than awaiting forever. Reuses the CLI turn ceiling (2h).
INTERACTIVE_TASK_MAX_S = float(getattr(config, "INTERACTIVE_TASK_MAX_S", config.CLAUDE_TIMEOUT))


class _InteractiveSessionDied(RuntimeError):
    """The interactive worker's PTY ended before the turn-end signal.

    ``had_viewer`` distinguishes a deliberate user stop (someone was watching
    the PTY and closed/killed the CLI → the run reports "user_interrupted")
    from an unattended crash (plain "failed")."""

    def __init__(self, message: str, *, had_viewer: bool = False):
        super().__init__(message)
        self.had_viewer = had_viewer


async def _run_interactive_task(
    session_id: str, chat_id: str, prompt: str, first_prompt_in_argv: bool,
) -> None:
    """Drive a FRESH interactive task to completion.

    There is no pump for an interactive session (the PTY emits raw bytes, not
    CommonEvents), so completion is detected from the turn-end signal: the
    transcript/rollout tailer (already running on the output-quiet debounce +
    close + reaper sweep) fires ``interactive_session.on_turn_complete`` once a
    turn ends with the bg ``SubagentRegistry`` empty + min-turn-time elapsed. We
    register that callback, inject the cold first prompt, and await it under a
    hard max-time backstop. The tailer has already persisted the turns to
    ``chat_messages`` by the time it fires, so the caller's ``_collect_task_output``
    + ``update_run`` + delivery work unchanged."""
    from core.session import interactive_session

    isess = interactive_session.get(session_id)
    if isess is None:
        raise RuntimeError("interactive task session was not registered")

    done = asyncio.Event()

    def _on_complete(_last_message: str) -> None:
        done.set()

    # Register BEFORE injecting the prompt so no turn can complete unobserved.
    isess.on_turn_complete = _on_complete
    # Cold first prompt: Codex fresh delivered it via the launch argv (auto-runs
    # after MCP warm); Claude needs the PTY flush (buffered behind the readiness
    # gate until the TUI accepts input). A trailing CR submits.
    if not first_prompt_in_argv:
        isess.submit_prompt(prompt)

    # Wait for the turn-end signal — but FAIL FAST if the PTY dies (CLI crash /
    # idle reap) instead of hanging until the max-time backstop. Poll liveness
    # between short waits on the completion event. (Without this, a Codex exit-1
    # on a bad config left the run stuck "running" for the full timeout.)
    waited = 0.0
    _STEP = 5.0
    while True:
        try:
            await asyncio.wait_for(done.wait(), timeout=_STEP)
            return  # turn completed (on_turn_complete fired)
        except asyncio.TimeoutError:
            if not isess.alive:
                raise _InteractiveSessionDied(
                    "Interactive task session ended before completing "
                    "(the CLI exited or was reaped)",
                    had_viewer=isess.had_viewer,
                )
            waited += _STEP
            if waited >= INTERACTIVE_TASK_MAX_S:
                raise RuntimeError(
                    f"Interactive task did not complete within "
                    f"{int(INTERACTIVE_TASK_MAX_S)}s (no turn-end signal)"
                )


async def _close_interactive_task_session(session_id: str) -> None:
    """Tear down an interactive task's PTY session (no-op if it wasn't one).

    ``layer.close_session`` only closes pump/daemon sessions — an interactive
    session lives in ``core.session.interactive_session`` — so completion/cancel/failure
    cleanup must close it here too (its final tail persists any tail-end output,
    then the slot is released)."""
    try:
        from core.session import interactive_session
        await interactive_session.close_session(session_id, reason="task_end")
    except Exception:
        logger.exception("interactive task %s: session close failed", session_id[:8])


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class RetryPolicy(BaseModel):
    max_attempts: int = 1
    delay_seconds: int = 60


class TaskDefinition(BaseModel):
    id: str
    name: str
    schedule: str = ""
    run_at: str | None = None
    delay_seconds: int | None = None
    # Recurring every N seconds. Mutually exclusive with schedule and run_at.
    # Drives IntervalTrigger; anchored at `created_at + interval_seconds`.
    interval_seconds: int | None = None
    agent: str
    llm_mode: str = "cli"
    prompt: str
    extra_context: list[str] = []
    retry: RetryPolicy = RetryPolicy()
    timeout_seconds: int = 600
    enabled: bool = True
    created_by: str | None = None
    # ISO UTC timestamp of row creation. Drives IntervalTrigger.start_date so
    # the cadence is deterministic across proxy restarts.
    created_at: str | None = None
    on_complete_agent: str | None = None
    on_complete_prompt: str | None = None
    on_complete_session_id: str | None = None
    continue_session: str | None = None
    use_persistent: bool = False
    # Required at the API boundary (Literal['auto','manual','none']).
    # Default exists only so internal constructions (e.g. _row_to_task on
    # historical rows) don't blow up if a row's value is somehow NULL.
    notification_mode: str = "manual"
    notify_severity: str = "info"
    scope: str = "user"
    # IANA timezone snapshotted at creation. Drives the trigger's TZ for
    # cron schedules and the parse-context for naive run_at ISO strings.
    # NULL → fall back to platform TZ (preserves behaviour for static and
    # pre-migration dynamic tasks).
    user_tz: str | None = None
    # 'scheduled' (cron / interval), 'one_time' (run_at / delay), or 'trigger'
    # (only fired by a webhook trigger; no schedule, no run_at). Auto-derived
    # if left empty: schedule or interval_seconds → scheduled, run_at → one_time.
    task_type: str = ""
    # Delegation surface="chat": run the turn INSIDE this existing chat instead
    # of a fresh task-<run_id> chat. The row must already exist (the spawn path
    # creates it with owner/origin/parent stamped).
    target_chat_id: str | None = None
    # Delegation lineage for fresh task-surface workers: the delegating chat
    # and project stamped onto the run's chat row so the dock's lane graph and
    # continuation authority can trace it. In-memory only, like the overrides.
    parent_chat_id: str | None = None
    project_id: str | None = None
    # Delegate-spawn per-lane execution overrides (validated against the
    # agent's envelope at spawn). In-memory only: delegates fire immediately
    # with THIS object — not persisted on dynamic_tasks, so a proxy-restart
    # retry re-resolves agent defaults. None = inherit.
    override_model: str | None = None
    override_execution_path: str | None = None
    override_execution_mode: str | None = None
    # Bounded recurring continuations (task_type='continuation'): hard fire
    # bound and/or stop time — a chat must never wake itself forever.
    max_runs: int | None = None
    until_at: str | None = None


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
        _dynamic_task_ids.add(task.id)
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
    global _shutting_down
    _shutting_down = True
    if not _running_tasks:
        logger.info("Scheduler shutdown: no running tasks")
        return

    count = len(_running_tasks)
    logger.info(f"Scheduler shutdown: cancelling {count} running task(s)")
    for run_id, task in list(_running_tasks.items()):
        if not task.done():
            task.cancel()

    # Wait for tasks to acknowledge cancellation
    tasks = [t for t in _running_tasks.values() if not t.done()]
    if tasks:
        done, pending = await asyncio.wait(tasks, timeout=10)
        if pending:
            logger.warning(
                f"Scheduler shutdown: {len(pending)} task(s) didn't finish in 10s"
            )

    logger.info("Scheduler shutdown: all running tasks cancelled")


def _resolve_task_tz(task: TaskDefinition):
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


def _register_task(task: TaskDefinition) -> None:
    """Register task with APScheduler."""
    job_id = f"task_{task.id}"
    task_tz = _resolve_task_tz(task)

    # Trigger-only tasks don't get an APScheduler entry — they're fired by
    # the trigger fire path (services/scheduler/trigger_manager.py). Storing them
    # without an APScheduler job is the correct end state.
    if task.task_type == "trigger":
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
        _fire_task,
        trigger=trigger,
        id=job_id,
        args=[task],
        replace_existing=True,
        misfire_grace_time=300,
        coalesce=True,
    )
    logger.info(f"Registered task: {task.id} (agent={task.agent})")


def _row_to_task(row: dict) -> TaskDefinition:
    """Convert a dynamic_tasks DB row to a TaskDefinition."""
    created_at = row.get("created_at")
    if created_at is not None and not isinstance(created_at, str):
        # psycopg may return datetime; normalise to ISO string for the model
        created_at = created_at.isoformat()
    return TaskDefinition(
        id=row["id"],
        name=row["name"],
        agent=row["agent"],
        llm_mode=row.get("llm_mode", "cli"),
        prompt=row["prompt"],
        schedule=row.get("schedule") or "",
        run_at=row.get("run_at"),
        delay_seconds=row.get("delay_seconds"),
        interval_seconds=row.get("interval_seconds"),
        timeout_seconds=row.get("timeout_seconds", 600),
        enabled=bool(row.get("enabled", 1)),
        created_by=row.get("created_by"),
        created_at=created_at,
        on_complete_agent=row.get("on_complete_agent"),
        on_complete_prompt=row.get("on_complete_prompt"),
        on_complete_session_id=row.get("on_complete_session_id"),
        continue_session=row.get("continue_session"),
        use_persistent=bool(row.get("use_persistent", 0)),
        notification_mode=row.get("notification_mode") or "manual",
        notify_severity=row.get("notify_severity", "info"),
        scope=row.get("scope", "user"),
        user_tz=row.get("user_tz"),
        task_type=row.get("task_type", ""),
        target_chat_id=row.get("target_chat_id"),
        max_runs=row.get("max_runs"),
        until_at=row.get("until_at"),
    )


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
    return [{"run_id": rid} for rid in _running_tasks]


def get_all_task_definitions() -> list[TaskDefinition]:
    """Return all dynamic tasks."""
    return [_row_to_task(row) for row in task_store.list_dynamic_tasks(enabled_only=False)]


async def trigger_task_now(task: TaskDefinition, trigger_type: str = "manual",
                           trigger_source: str | None = None,
                           prompt_override: str | None = None,
                           trigger_payload: dict | None = None) -> str:
    """Immediately execute a task. Returns run_id.

    ``trigger_payload``: set by ``trigger_manager`` when this
    task was fired by a webhook trigger. Carries the normalised event data
    that downstream ``agent_context`` builder blocks resolve via
    ``${trigger.*}`` tokens. ``None`` for manual / scheduled fires.
    """
    return await _execute_task(task, trigger_type=trigger_type,
                               trigger_source=trigger_source,
                               prompt_override=prompt_override,
                               trigger_payload=trigger_payload)


async def add_dynamic_task(task: TaskDefinition) -> str:
    """Persist to DB + register with APScheduler live. Returns task.id."""
    # Resolve task_type: explicit value wins; otherwise auto-derive.
    # Recurring = cron schedule OR interval_seconds. One-time = run_at/delay.
    task_type = task.task_type or (
        "scheduled"
        if (task.schedule or task.interval_seconds is not None)
        else "one_time"
    )
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
    )
    _dynamic_task_ids.add(task.id)
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
    if config.SCHEDULER_MODE != "standalone" and task_type != "trigger":
        # Update the in-memory task with the resolved task_type so _register_task
        # sees it (rare path: TaskDefinition created without task_type set).
        if not task.task_type:
            task.task_type = task_type
        _register_task(task)
    return task.id


async def remove_dynamic_task(task_id: str) -> bool:
    """Remove from DB + unschedule from APScheduler."""
    deleted = await asyncio.to_thread(task_store.delete_dynamic_task, task_id)
    _dynamic_task_ids.discard(task_id)
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
        payload["task_type"] = "scheduled"
    elif payload.get("interval_seconds"):
        payload["schedule"] = None
        payload["run_at"] = None
        payload["task_type"] = "scheduled"
    elif payload.get("run_at"):
        payload["schedule"] = None
        payload["interval_seconds"] = None
        payload["task_type"] = "one_time"

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
    t = _running_tasks.get(run_id)
    if t and not t.done():
        _user_cancelled_runs.add(run_id)
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
    t = _running_tasks.get(run_id)
    if t and not t.done():
        _platform_interrupts[run_id] = reason
        t.cancel()
        return True
    return False


async def subscribe_run(run_id: str) -> asyncio.Queue:
    q: asyncio.Queue = asyncio.Queue()
    # Replay buffered events so late subscribers (e.g. after a page refresh) catch up
    for event in _run_event_buffer.get(run_id, []):
        await q.put(event)
    _run_subscribers.setdefault(run_id, []).append(q)
    return q


def unsubscribe_run(run_id: str, q: asyncio.Queue) -> None:
    subs = _run_subscribers.get(run_id, [])
    if q in subs:
        subs.remove(q)
    if not subs:
        _run_subscribers.pop(run_id, None)


async def _deliver_task_result(task: TaskDefinition, final_status: str, output_text: str,
                               *, worker_chat_id: str = "", output_cursor: int = 0,
                               prompt_row_id: int = 0, prompt_text: str = "") -> None:
    """Deliver completed task result to originating session/chat.

    The originating ``on_complete_session_id`` is the primary route key
    (matches the in-memory notify-queue / pump tables). ``on_complete_chat_id``
    is the persistent anchor that survives browser close / proxy restart;
    when the original session is gone, ``_do_deliver`` walks the chat row
    to find the user's current session and routes there instead.

    ``worker_chat_id`` (the run's own chat) turns on LANE FINALIZATION inside
    the spawned delivery task — after the run releases its RAM slot: await
    lane quiescence (a user may still be steering the worker), re-check the
    chat's abort flag, and re-collect cursor-based so the callback carries the
    complete round including ``[User interjected]`` rows. It also feeds the
    ``{{chat_id}}`` template token.
    """
    row = await asyncio.to_thread(task_store.get_dynamic_task, task.id)
    on_complete_agent = (row.get("on_complete_agent") if row else None) or task.on_complete_agent
    on_complete_prompt = (row.get("on_complete_prompt") if row else None) or task.on_complete_prompt
    on_complete_session_id = (row.get("on_complete_session_id") if row else None) or task.on_complete_session_id
    on_complete_chat_id = (row.get("on_complete_chat_id") if row else None) if row else None

    if not on_complete_agent or not on_complete_prompt:
        logger.info(
            f"Task result delivery skipped (no on_complete): task={task.id}, "
            f"agent={on_complete_agent}, prompt={'yes' if on_complete_prompt else 'no'}, "
            f"session={on_complete_session_id}, chat={on_complete_chat_id}, "
            f"row={'found' if row else 'missing'}"
        )
        return

    # Resolve session_id from chat_id if we have a chat but no/stale session
    if on_complete_chat_id and not on_complete_session_id:
        chat = await asyncio.to_thread(task_store.get_chat, on_complete_chat_id)
        if chat:
            on_complete_session_id = chat.get("session_id")

    if not on_complete_session_id:
        logger.info(f"Task result delivery skipped (no session): task={task.id}")
        return

    logger.info(
        f"Task result delivery starting: task={task.id}, "
        f"session={on_complete_session_id[:8]}, chat={on_complete_chat_id or 'none'}"
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
                if status == "user_interrupted":
                    await _await_lane_quiescence(
                        worker_chat_id,
                        ceiling_seconds=1800.0,
                        settle_seconds=120.0,
                        immediate_quiet_ok=False,
                    )
                else:
                    await _await_lane_quiescence(
                        worker_chat_id, ceiling_seconds=1800.0,
                    )
                chat = await asyncio.to_thread(task_store.get_chat, worker_chat_id)
                if chat and chat.get("last_turn_aborted"):
                    status = "user_interrupted"
                collected = await asyncio.to_thread(
                    _collect_lane_output_since, worker_chat_id,
                    output_cursor, prompt_row_id, prompt_text,
                )
                if collected:
                    output = collected
            except Exception:
                logger.exception(
                    f"Lane finalization failed for chat {worker_chat_id[:8]} — "
                    f"delivering the run's own collection"
                )
        result_prompt = (on_complete_prompt
            .replace("{{output}}", output or "")
            .replace("{{task_id}}", task.id)
            .replace("{{task_name}}", task.name)
            .replace("{{status}}", status)
            .replace("{{chat_id}}", worker_chat_id or "")
            .replace("{{agent}}", task.agent))
        await _do_deliver(
            on_complete_session_id, on_complete_agent, result_prompt, task,
            chat_id=on_complete_chat_id,
            output_text=output,
            status=status,
        )

    asyncio.create_task(_finalize_and_deliver())


async def _do_deliver(session_id: str, agent: str, result_prompt: str, task: TaskDefinition,
                      *, chat_id: str | None = None, output_text: str = "",
                      status: str = "completed") -> None:
    """Deliver a delegate result via the session-delivery ladder.

    The routing itself (WS notify → pump → persistent → one-shot) lives in
    ``core.session.session_delivery.deliver_prompt``; this wrapper owns the
    DELEGATE semantics — the ``delegate_result`` event payload, the live-state
    badge, the ``task_result_prompt`` notify shape, and the echo persistence.
    The persistent/one-shot rungs are passed by name so they resolve through
    this module at call time (tests monkeypatch them here).
    """
    try:
        from core.session.session_delivery import deliver_prompt
        from core.session.session_state import mark_delegate_completed

        # Update live state immediately (for reconnect accuracy)
        if chat_id:
            mark_delegate_completed(chat_id, task.name, task_id=task.id, status=status)

        # Delegate event data shared across all paths. task_id is the stable
        # spawn↔result correlation key (the frontend keys the delegate block by it).
        # status (completed/failed/cancelled) drives the badge icon + lets the
        # frontend skip minting an empty bubble for a no-output terminal.
        output_preview = (output_text or "")[:2000]
        delegate_event = {
            "type": "delegate_result",
            "task_id": task.id,
            "task_name": task.name,
            "agent": task.agent,
            "output_text": output_preview,
            "status": status,
        }
        delegate_event_data = json.dumps({
            "task_id": task.id,
            "task_name": task.name,
            "agent": task.agent,
            "output_text": output_preview,
            "status": status,
        })

        def _persist_delegate_event(target_chat_id: str) -> None:
            # The delegate_result event is the source of truth — it renders the
            # delegate's output and completes the badge regardless of whether
            # the LLM-echo delivery succeeds. Invoked exactly once by the
            # ladder on every non-WS path (the dashboard handler persists it
            # on the WS path after rendering).
            if target_chat_id:
                task_store.add_chat_message(target_chat_id, "event", "",
                    event_type="delegate_result",
                    event_data=delegate_event_data)

        # Resolve with the originating session's user + role so a user-pinned
        # remote target (or admin-default remote) is honored on the
        # persistent/one-shot rungs — a bare resolve would default to local and
        # miss a user override / break remote start.
        if task.scope == "user" and task.created_by:
            deliver_user_sub: str | None = task.created_by
            deliver_role = task_store.get_user_agent_roles(task.created_by).get(agent, "viewer")
        else:
            deliver_user_sub = None
            deliver_role = "manager"

        def _save_echo(outcome) -> None:
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
                task_store.add_chat_message(outcome.chat_id, "assistant", outcome.response)
                logger.info(f"Task result echo saved to chat DB: chat={outcome.chat_id[:8]}, task={task.name}")
            else:
                from core.session import session_state as _state_mod
                _state_mod._save_pending_result(outcome.session_id, outcome.response)
                logger.info(f"Task result echo saved as pending: session={outcome.session_id[:8]}, task={task.name}")

        outcome = await deliver_prompt(
            chat_id or "", result_prompt,
            source="delegate_result",
            session_id=session_id,
            agent=agent,
            user_sub=deliver_user_sub,
            role=deliver_role,
            notify_payload={
                "type": "task_result_prompt",
                # Originating (delegating) chat/session so the dashboard handler
                # runs the synthesis turn on THIS chat — not whatever the socket
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
            },
            pump_event=delegate_event,
            persist_event=_persist_delegate_event,
            persistent_fn=_deliver_via_persistent,
            oneshot_fn=_deliver_via_oneshot,
            on_outcome=_save_echo,
        )
        logger.info(
            f"Task result delivered via {outcome.path}: "
            f"session={outcome.session_id[:8] if outcome.session_id else '-'}, task={task.name}"
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

        # Durable replay: every rung failed — the orchestrator was never woken
        # (the delegate_result event row IS persisted, but no turn ran). Store
        # the wake on the parent chat; the next warmup/turn claims and injects
        # it so the orchestrator continues then (a manual re-warm included).
        if chat_id and outcome.path == "none":
            stored = task_store.append_pending_delegate_wake(chat_id, result_prompt)
            if stored:
                logger.info(
                    f"Delegate wake undeliverable — stored for replay on next "
                    f"warmup: chat={chat_id[:8]}, task={task.name}"
                )
    except Exception as e:
        logger.error(f"Task result delivery failed: session={session_id[:8]}, task={task.id}: {e}", exc_info=True)


async def redeliver_pending_wakes(machine_id: str | None = None) -> int:
    """Redeliver delegate wakes parked on chats (stored when every delivery
    rung failed — typically the proxy restarted inside the delivery window).

    Startup runs it once after boot settles; every satellite reconnect runs it
    scoped to that machine's pinned chats (an idle satellite chat's wake can
    only deliver once its machine is back). Per-chat claims are atomic, so a
    concurrent warmup/turn claim wins cleanly and this pass no-ops for that
    chat. A chat whose session is parked for Mode C re-adopt is skipped —
    the reconnect pass retries it after the adopt settles. Failed deliveries
    re-persist the original wakes (same caller-side contract as
    ``_deliver_task_result``). Returns the number of chats woken."""
    from services.scheduler import run_recovery
    from core.session.session_delivery import deliver_prompt

    rows = await asyncio.to_thread(
        task_store.list_chats_with_pending_wakes, machine_id,
    )
    woken = 0
    for chat in rows:
        chat_id = chat["id"]
        if (chat.get("session_id") or "") in run_recovery._parked:
            continue
        wakes = await asyncio.to_thread(
            task_store.claim_pending_delegate_wake, chat_id,
        )
        if not wakes:
            continue
        agent = chat.get("agent") or ""
        owner = chat.get("user_sub") or ""
        # ``task::`` owners are the unified task chats — agent-scope delivery
        # semantics, never a personal remote-target pin.
        if owner and not owner.startswith("task::"):
            deliver_user_sub: str | None = owner
            role = (await asyncio.to_thread(
                task_store.get_user_agent_roles, owner,
            )).get(agent, "viewer")
        else:
            deliver_user_sub = None
            role = "manager"

        def _save_echo(outcome) -> None:
            # Same contract as _deliver_task_result's echo hook: pump-driven
            # turns return "" (already persisted); dead resumes return None.
            if outcome.response and outcome.chat_id:
                task_store.add_chat_message(
                    outcome.chat_id, "assistant", outcome.response,
                )

        try:
            outcome = await deliver_prompt(
                chat_id, "\n\n".join(wakes),
                source="delegate_result_replay",
                session_id=chat.get("session_id") or "",
                agent=agent,
                user_sub=deliver_user_sub,
                role=role,
                persistent_fn=_deliver_via_persistent,
                oneshot_fn=_deliver_via_oneshot,
                on_outcome=_save_echo,
            )
        except Exception:
            # One chat's broken spawn (bad config, dead layer) must neither
            # kill the sweep nor eat the CLAIMED wakes — re-persist and move
            # on (live-hit on T1: a corrupt user config.toml failed the
            # oneshot and the claimed wake was lost with the sweep).
            logger.exception(
                "Wake redelivery: chat %s delivery raised — re-storing %d "
                "wake(s)", chat_id[:8], len(wakes),
            )
            for w in wakes:
                await asyncio.to_thread(
                    task_store.append_pending_delegate_wake, chat_id, w,
                )
            continue
        if outcome.path == "none":
            for w in wakes:
                await asyncio.to_thread(
                    task_store.append_pending_delegate_wake, chat_id, w,
                )
            logger.info(
                "Wake redelivery: chat %s undeliverable — re-stored %d wake(s)",
                chat_id[:8], len(wakes),
            )
        else:
            woken += 1
            logger.info(
                "Wake redelivery: chat %s woken via %s (%d wake(s))",
                chat_id[:8], outcome.path, len(wakes),
            )
    return woken


async def _run_echo_turn_pumped(layer, session_id: str, chat_id: str,
                                agent: str, result_prompt: str) -> str | None:
    """Run a delegate-echo turn through a headless ChatStreamPump on the
    delegating chat.

    The old direct collection (``layer.send_message`` → join TEXT parts) ran
    the turn INVISIBLY: no ``chat_status`` streaming/ready broadcasts, no live
    pump a viewer could attach to, no ``last_response_at`` stamp (so no unread
    dot or Active-now row), and every non-text block dropped — a delivered
    result nobody was told about (2026-07-13 incident, chat 75eab195). The
    pump is the one turn pipeline that does all of that, and a viewer opening
    the chat mid-turn attaches to it like any streaming chat.

    Returns "" on success — the pump persisted the turn itself, so the caller
    must NOT save an echo row — or None when a pump can't run here (the caller
    falls through / logs the refusal). A stalled turn is reaped by the task
    watchdog with its partial output persisted; that still counts as delivered
    (the delegate_result event row is the source of truth either way).
    """
    from core.events.stream_pump import ChatStreamPump, _active_pumps
    from core.events.task_producer import task_produce
    from core.session import visibility as _vis
    from core.session.session_state import get_permission_queue

    if _active_pumps.get(chat_id) is not None:
        # A turn started on the chat between the ladder's pump check and now —
        # never dual-pump a chat.
        return None
    echo_id = f"echo-{chat_id[:8]}"
    event_queue: asyncio.Queue = asyncio.Queue()
    producer = asyncio.create_task(task_produce(
        layer, session_id, result_prompt, event_queue, echo_id,
        settle_timeout=30.0,
    ))
    pump = ChatStreamPump(
        chat_id=chat_id,
        session_id=session_id,
        producer=producer,
        event_queue=event_queue,
        perm_queue=get_permission_queue(session_id),
        scope="agent" if _vis.is_shared_only(agent) else "user",
    )
    _active_pumps[chat_id] = pump
    pump.start()
    # A _TaskTurnStalled reap persisted the partial turn and closed the chat
    # status — delivered as far as it went; never fall through to another rung
    # (that would run a SECOND echo turn).
    with contextlib.suppress(_TaskTurnStalled):
        await _watch_task_pump(layer, pump, echo_id, chat_id, session_id)
    return ""


async def _deliver_via_persistent(
    session_id: str, agent: str, result_prompt: str,
    *, user_sub: str | None = None, role: str = "manager", chat_id: str = "",
) -> str | None:
    """Path A/B: Use alive persistent session.

    With a delegating chat the echo turn runs through a headless pump (see
    ``_run_echo_turn_pumped``) and returns "" — the pump persisted it.
    Chat-less deliveries keep the direct collection so the caller can save the
    response as a pending result. None = this rung could not deliver."""
    from core.session import session_state as _state
    from core.session.session_manager import get_execution_layer
    from core.events.common_events import TEXT

    try:
        layer = get_execution_layer(agent, user_sub=user_sub, role=role)
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
                layer, session_id, chat_id, agent, result_prompt,
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
    except Exception:
        # Session broken (BrokenPipeError, OSError, RuntimeError, etc.)
        await layer.close_session(session_id)
        return None


async def _deliver_via_oneshot(
    session_id: str, agent: str, result_prompt: str,
    *, user_sub: str | None = None, role: str = "manager", chat_id: str = "",
) -> str | None:
    """Path C: Dead session, use one-shot --resume via execution layer.

    With a delegating chat the resumed turn runs through a headless pump (see
    ``_run_echo_turn_pumped``) and returns "" — the pump persisted it.
    Chat-less deliveries keep the direct collection so the caller can save the
    response as a pending result."""
    from core.session import session_state as _state
    from core.session.session_manager import get_execution_layer, resolve_execution_path
    from core.events.common_events import TEXT
    from services.mcp import mcp_registry
    from storage import remote_store

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
    target, _reason = remote_store.resolve_execution_target(agent, user_sub, role)
    if target.startswith("__offline__:"):
        logger.warning(
            f"Delegate one-shot delivery skipped — agent '{agent}' remote target "
            f"offline: session={session_id[:8]}"
        )
        return None
    layer = get_execution_layer(
        agent, execution_path=exec_path, user_sub=user_sub, role=role,
        execution_target=target,
    )
    # Resumability pre-check: if the session has no conversation file to
    # --resume, the oneshot would emit a "No conversation found" error that we'd
    # otherwise collect and save as the delegate's response. Skip instead — the
    # delegate_result event (saved by _do_deliver) already carries the output.
    username = task_store.get_username_by_sub(user_sub) if user_sub else ""
    if not await layer.can_resume_session(session_id, agent_name=agent, username=username or ""):
        logger.info(
            f"Delegate one-shot delivery skipped — session {session_id[:8]} has no "
            f"resumable conversation (agent={agent})"
        )
        return None
    from core.execution_layer import AgentConfig
    from core.config.task_config_builder import build_delivery_security_context
    # Device-local MCP placement facts for this (possibly remote) one-shot.
    # `target` is already resolved above (local | machine_id; offline returned
    # earlier), so derive is_remote / display and thread them so a remote
    # agent's device MCPs aren't dropped from the resume config + prompt.
    _os_kind, _ = remote_store.get_target_metadata(target, user_sub, agent)
    _os_is_remote = _os_kind in ("admin_remote", "user_remote")
    _os_has_display = remote_store.get_target_has_display(_os_kind, target)
    _os_grants = remote_store.get_target_device_grants(_os_kind, target)
    agent_prompt = config.build_agent_prompt(
        agent, client_type="task",
        is_remote=_os_is_remote, target_has_display=_os_has_display,
        target_device_grants=_os_grants,
    )
    mcp_config, _, _, _, _ = mcp_registry.build_session_mcp_config(
        agent, None, task_mode=True, task_scope="agent",
        is_remote=_os_is_remote, target_has_display=_os_has_display,
        target_device_grants=_os_grants,
    )
    # A LOCAL one-shot resume MUST run sandboxed + network-isolated like every
    # other session — the local layers fail closed without a sandbox dir. Resolve
    # the SAME persistent config dir the session was built with (user scope when
    # the chat is user-owned, else agent/workspace) so --resume finds its
    # conversation. Remote targets ignore this (the satellite owns the config).
    oneshot_claude_dir = ""
    if not _os_is_remote:
        from core.sandbox.sandbox import ensure_persistent_agent_dir
        _hcd = await asyncio.to_thread(
            ensure_persistent_agent_dir, agent,
            execution_path=exec_path,
            username=username or "",
            scope="user" if username else "agent",
        )
        oneshot_claude_dir = str(_hcd)
    # Close/reap dropped the session's security context (JWT-replay defense) —
    # rebuild it for the resumed turn or every hook fail-closes with
    # "Session is no longer active" and the callback runs tool-dead.
    oneshot_security = await build_delivery_security_context(
        agent, user_sub=user_sub, role=role, target=target,
    )
    oneshot_cfg = AgentConfig(
        agent_name=agent,
        user_sub=user_sub or "",
        system_prompt=agent_prompt or "",
        mcp_config_path=str(mcp_config) if mcp_config else "",
        permission_mode="auto",
        client_type="",
        resume=True,
        security_context=oneshot_security,
        execution_target=target,
        execution_path=exec_path,
        sandbox_host_claude_dir=oneshot_claude_dir,
        # The CHAT's pinned model — an empty model falls back to the AGENT
        # default, which belongs to the agent's default LAYER; on a chat
        # pinned to the other CLI that yields an unusable/empty model
        # ("API Error: 400 model: String should have at least 1 character").
        model=(_chat_row or {}).get("model") or "",
    )
    # A chat pinned interactive gets its wake in ITS OWN MODE: re-warm the
    # terminal session, inject the result, await the orchestrator's turn,
    # close (unless a viewer attached meanwhile). The headless echo below
    # would run the wake invisibly outside the terminal the chat lives in
    # (previously: "interactive resume/delegation is future work"). On any
    # failure return None — the caller's path="none" stores the wake for
    # durable replay at the chat's next warmup.
    from core import execution_mode as _exec_mode
    chat_row = _chat_row
    if chat_row and _exec_mode.is_interactive(
        chat_override=chat_row.get("execution_mode") or None,
    ):
        return await _rewarm_interactive_and_wake(
            layer, session_id, agent, result_prompt,
            chat_id=chat_id, base_cfg=oneshot_cfg, chat_row=chat_row,
        )
    await layer.start_session(session_id, oneshot_cfg)
    if chat_id:
        return await _run_echo_turn_pumped(
            layer, session_id, chat_id, agent, result_prompt,
        )
    parts: list[str] = []
    async with layer.session_lock(session_id):
        async for event in layer.send_message(session_id, result_prompt):
            if event.type == TEXT:
                content = event.data.get("content", "")
                if content:
                    parts.append(content)
    return "".join(parts) if parts else ""


# Turn-open verification window for interactive wakes: covers CLI spawn →
# readiness gate → paste flush → one transcript-tail poll of lag. Two windows
# run back to back (initial submit, then the bare-Enter nudge).
_WAKE_TURN_OPEN_S = 45.0


async def _wait_turn_open(isess, timeout: float) -> bool:
    """Poll until the session's transcript-derived turn-open flag flips."""
    waited = 0.0
    while waited < timeout:
        if isess.turn_open:
            return True
        if not isess.alive:
            return False
        await asyncio.sleep(1.0)
        waited += 1.0
    return bool(isess.turn_open)


async def _rewarm_interactive_and_wake(
    layer, session_id: str, agent: str, result_prompt: str,
    *, chat_id: str, base_cfg, chat_row: dict,
) -> str | None:
    """Re-warm a DEAD interactive-pinned chat and inject the delegate wake.

    The interactive sibling of the headless one-shot: resume the chat's
    session as a PTY (``interactive=True``, chat-bound, the chat's own
    permission mode + baked TUI theme), submit the wake with the settle
    Enter, VERIFY the turn opened (transcript-derived — an unverified submit
    silently loses the payload), await the orchestrator's continuation turn
    (``_run_interactive_task`` shape: turn-end callback + liveness poll + max
    backstop), then leave the terminal to the idle reaper so a user opening
    the chat shortly after still attaches to it. The chat-bound tailer
    persists the whole exchange to chat history as it runs.

    A ``warmup_registry`` entry brackets the spawn so a racing delivery's
    ``_oneshot_blocked`` defers to this re-warm and a dashboard opening the
    chat mid-wake attaches instead of double-spawning.

    Returns "" on success (the tailer persisted the turn — no echo row to
    save) or None on any failure (the caller stores the wake for durable
    replay)."""
    from core.session import interactive_session, warmup_registry

    cfg = base_cfg
    cfg.interactive = True
    # The chat binding is LOAD-BEARING: the InteractiveSession registers with
    # cfg.chat_id, and transcript tailing → chat_messages, the turn-open /
    # turn-complete signals, the streaming-status broadcasts AND the
    # dashboard's pty_attach chat guard all gate on it. Without it the wake
    # turn runs invisibly and unpersisted, and an open chat refuses to attach
    # to the live terminal (the original wake-recovery shipped that way).
    cfg.chat_id = chat_id
    # The wake turn runs unattended — keep the task-grade "auto" permission
    # mode from base_cfg (a "default"-mode prompt would sit unanswered with
    # nobody watching; same rationale as headless task re-warms).
    cfg.interactive_theme = chat_row.get("tui_theme") or ""

    registered = False
    spawned = False
    try:
        try:
            await warmup_registry.register(chat_id, cfg.user_sub or "", agent)
            registered = True
        except Exception:
            pass  # registry is an optimization, never a spawn blocker

        await layer.start_session(session_id, cfg)
        spawned = True
        isess = interactive_session.get(session_id)
        if isess is None:
            logger.warning(
                f"Delegate interactive re-warm: no PTY registered for "
                f"session={session_id[:8]} (chat={chat_id[:8]})"
            )
            return None

        done = asyncio.Event()
        isess.on_turn_complete = lambda _msg: done.set()
        # Cold wake prompt: buffered behind the readiness gate, flushed with
        # the settle Enter (large pastes were losing the fixed-gap Enter).
        isess.submit_prompt(result_prompt, settle=True)
        logger.info(
            f"Delegate wake re-warmed interactive: session={session_id[:8]}, "
            f"chat={chat_id[:8]}, agent={agent}"
        )
        if registered:
            # Spawn is live — release the registry so the queued-prompt drain
            # and dashboard attach gates stop deferring to it.
            await warmup_registry.unregister(chat_id)
            registered = False

        # Verify the turn actually OPENED before counting the wake delivered.
        # turn_open is transcript-derived (both CLIs write the user line at
        # submit), so a paste that never submitted leaves it False while the
        # PTY idles — previously that "delivery" silently lost the payload.
        # One bare-Enter nudge covers a paste that ate its settle Enter.
        if not await _wait_turn_open(isess, _WAKE_TURN_OPEN_S):
            if isess.alive:
                isess.submit_prompt("", settle=True)
            if not await _wait_turn_open(isess, _WAKE_TURN_OPEN_S):
                logger.warning(
                    f"Delegate wake never opened a turn (paste lost?): "
                    f"session={session_id[:8]}, chat={chat_id[:8]} — storing "
                    f"for durable replay"
                )
                if isess.alive and not isess.has_viewer:
                    await interactive_session.close_session(
                        session_id, reason="delegate_wake_unsubmitted",
                    )
                return None

        waited = 0.0
        _STEP = 5.0
        while True:
            try:
                await asyncio.wait_for(done.wait(), timeout=_STEP)
                break  # orchestrator's continuation turn completed
            except asyncio.TimeoutError:
                if not isess.alive:
                    logger.warning(
                        f"Delegate wake interactive session died mid-turn: "
                        f"session={session_id[:8]} (chat={chat_id[:8]})"
                    )
                    # The turn opened; the tailer persisted whatever ran.
                    # Treat as delivered — a re-store would double-wake.
                    return ""
                waited += _STEP
                if waited >= INTERACTIVE_TASK_MAX_S:
                    logger.warning(
                        f"Delegate wake turn exceeded {int(INTERACTIVE_TASK_MAX_S)}s "
                        f"backstop: session={session_id[:8]} — leaving session to "
                        f"the idle reaper"
                    )
                    return ""

        # Turn done. Leave the terminal ALIVE either way — the idle reaper
        # owns its end. A user opening the chat within the idle window
        # attaches to the real terminal with the wake turn on screen instead
        # of finding it torn down the instant the turn ended.
        logger.info(
            f"Delegate wake turn complete: session={session_id[:8]}, "
            f"viewer={'yes' if isess.has_viewer else 'no'} — terminal left "
            f"to the idle reaper"
        )
        return ""
    except Exception:
        logger.exception(
            f"Delegate interactive re-warm failed: session={session_id[:8]}, "
            f"chat={chat_id[:8]}"
        )
        if spawned:
            try:
                isess = interactive_session.get(session_id)
                if isess is not None and not isess.has_viewer:
                    await interactive_session.close_session(
                        session_id, reason="delegate_wake_failed",
                    )
            except Exception:
                pass
        return None
    finally:
        if registered:
            with contextlib.suppress(Exception):
                await warmup_registry.unregister(chat_id)


async def _fire_task(task: TaskDefinition) -> None:
    """APScheduler calls this as an async job. Creates a fire-and-forget task."""
    asyncio.create_task(_execute_task(task, trigger_type="scheduled"))


def _determine_task_type(task: TaskDefinition, trigger_type: str) -> str:
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


async def _fire_continuation(task: TaskDefinition) -> str:
    """Fire one scheduled self-continuation: deliver the wake prompt INTO the
    target chat as a new turn (via the session-delivery ladder — live pump,
    PTY injection, dead-session resume all inherited). No run row, no task
    slot — the driven turn's cost lands on the chat like any other turn.

    Guards, in order: chat gone → self-cancel; past ``until_at`` → self-cancel;
    coalesce (previous wake unprocessed → skip). Post-fire: one-shot rows
    auto-delete; recurring rows count fires and self-cancel at ``max_runs``."""
    from core.session.session_delivery import deliver_prompt

    chat_id = task.target_chat_id or ""
    chat = await asyncio.to_thread(task_store.get_chat, chat_id) if chat_id else None
    if not chat:
        logger.info(f"Continuation {task.id}: chat {chat_id[:8] or '-'} gone — cancelling")
        await remove_dynamic_task(task.id)
        _continuation_cursors.pop(chat_id, None)
        _continuation_skip_counts.pop(task.id, None)
        return ""

    if task.until_at:
        try:
            until = datetime.fromisoformat(task.until_at)
            if until.tzinfo is None:
                until = until.replace(tzinfo=_resolve_task_tz(task))
            if datetime.now(timezone.utc) >= until:
                logger.info(f"Continuation {task.id}: past until={task.until_at} — cancelling")
                await remove_dynamic_task(task.id)
                return ""
        except ValueError:
            logger.warning(f"Continuation {task.id}: bad until_at {task.until_at!r} — ignoring bound")

    last_id = await asyncio.to_thread(task_store.get_last_chat_message_id, chat_id)
    prev = _continuation_cursors.get(chat_id)
    if prev is not None and last_id <= prev:
        skips = _continuation_skip_counts.get(task.id, 0) + 1
        _continuation_skip_counts[task.id] = skips
        logger.info(
            f"Continuation {task.id}: previous wake still unprocessed on "
            f"chat {chat_id[:8]} — coalesced (skipped ×{skips})"
        )
        recurring = bool(task.schedule) or task.interval_seconds is not None
        if recurring and task.max_runs and skips > _SKIP_AGING_GRACE:
            new_count = await asyncio.to_thread(
                task_store.increment_dynamic_task_run_count, task.id,
            )
            if new_count >= task.max_runs:
                logger.info(
                    f"Continuation {task.id}: dead-chat skips reached "
                    f"max_runs={task.max_runs} — cancelling"
                )
                await remove_dynamic_task(task.id)
                _continuation_skip_counts.pop(task.id, None)
        return ""
    _continuation_skip_counts.pop(task.id, None)

    wake_prompt = task.prompt
    wake_event_data = json.dumps({"prompt": wake_prompt, "task_id": task.id})

    def _persist_wake(target_chat_id: str) -> None:
        if target_chat_id:
            task_store.add_chat_message(target_chat_id, "event", "",
                event_type="schedule_wake", event_data=wake_event_data)

    # Same identity resolution as delegate delivery: the chat owner's remote
    # pins / role must be honored on the persistent/one-shot rungs.
    if task.scope == "user" and task.created_by:
        deliver_user_sub: str | None = task.created_by
        deliver_role = task_store.get_user_agent_roles(task.created_by).get(task.agent, "viewer")
    else:
        deliver_user_sub = None
        deliver_role = "manager"

    def _save_echo(outcome) -> None:
        if not outcome.response:
            return
        if outcome.chat_id:
            task_store.add_chat_message(outcome.chat_id, "assistant", outcome.response)
        else:
            from core.session import session_state as _state_mod
            _state_mod._save_pending_result(outcome.session_id, outcome.response)

    try:
        outcome = await deliver_prompt(
            chat_id, wake_prompt,
            source="schedule_wake",
            session_id=chat.get("session_id") or "",
            agent=task.agent,
            user_sub=deliver_user_sub,
            role=deliver_role,
            notify_payload={
                "type": "continuation_prompt",
                "session_id": chat.get("session_id") or "",
                "chat_id": chat_id,
                "task_id": task.id,
                "task_name": task.name,
                "prompt": wake_prompt,
            },
            persist_event=_persist_wake,
            persistent_fn=_deliver_via_persistent,
            oneshot_fn=_deliver_via_oneshot,
            on_outcome=_save_echo,
        )
        logger.info(f"Continuation {task.id} fired via {outcome.path}: chat={chat_id[:8]}")
    except Exception:
        logger.exception(f"Continuation {task.id} delivery failed: chat={chat_id[:8]}")
    _continuation_cursors[chat_id] = await asyncio.to_thread(
        task_store.get_last_chat_message_id, chat_id,
    )

    if not task.schedule and task.interval_seconds is None:
        # One-shot wake — the row's purpose is served.
        await remove_dynamic_task(task.id)
    else:
        new_count = await asyncio.to_thread(
            task_store.increment_dynamic_task_run_count, task.id,
        )
        if task.max_runs and new_count >= task.max_runs:
            logger.info(f"Continuation {task.id}: reached max_runs={task.max_runs} — cancelling")
            await remove_dynamic_task(task.id)
    return ""


def _create_task_chat_row(chat_id: str, run_id: str, task: TaskDefinition) -> None:
    """The run's chat row + ``runs.chat_id`` stamp (sync — call via to_thread).

    Task-surface delegate workers keep the shared ``agent::`` owner for agent
    scope, the delegate name as seed title, the delegated origin (purple
    worker accent + LLM title upgrade) and the delegation lineage for the
    dock's lane graph; plain runs use the ``task::`` owner convention."""
    task_model = config.get_cli_model(task.agent)
    if task.task_type == "delegate":
        worker_sub = (task.created_by if task.scope == "user"
                      else f"agent::{task.agent}")
        task_store.create_chat(chat_id, worker_sub, task.agent, "auto",
                               model=task_model, origin="delegated",
                               parent_chat_id=task.parent_chat_id or "",
                               project_id=task.project_id or "",
                               delegate_role="worker",
                               title=task.name)
    else:
        user_sub = (task.created_by if task.scope == "user"
                    else f"task::{task.agent}")
        task_store.create_chat(chat_id, user_sub, task.agent, "auto",
                               model=task_model)
    task_store.update_run(run_id, chat_id=chat_id)


async def _execute_task(task: TaskDefinition, trigger_type: str = "scheduled",
                        trigger_source: str | None = None,
                        prompt_override: str | None = None, attempt: int = 1,
                        trigger_payload: dict | None = None) -> str:
    from core.session import session_state as _state

    # Continuations are wake deliveries, not LLM task runs — no run row, no
    # slot, no usage pre-check (the driven turn bills like any chat turn).
    if task.task_type == "continuation":
        return await _fire_continuation(task)

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
    _state._sessions[session_id]["last_active"] = now_iso()
    _state._save_sessions()
    if task.extra_context:
        final_prompt += "\n\n" + "\n\n".join(task.extra_context)

    # ── Usage limit check before execution ──
    try:
        from services.billing import usage_service
        if task.scope == "user" and task.created_by:
            creator = await asyncio.to_thread(task_store.get_user, task.created_by)
            creator_role = (creator or {}).get("role", "member")
            limit_status = await asyncio.to_thread(
                usage_service.check_user_limit, task.created_by, creator_role
            )
            if not limit_status["allowed"]:
                logger.warning(f"Task {task.id} blocked by user limit: {task.created_by}")
                task_type = _determine_task_type(task, trigger_type)
                await asyncio.to_thread(
                    task_store.create_run, run_id, task.id, task.agent,
                    trigger_type, trigger_source, final_prompt, task_type,
                    task.scope, task.created_by,
                )
                await asyncio.to_thread(
                    task_store.update_run, run_id,
                    status="limit_exceeded",
                    error_message="User usage limit exceeded",
                    completed_at=datetime.now(timezone.utc).isoformat(),
                )
                return run_id
        elif task.scope == "agent":
            limit_status = await asyncio.to_thread(
                usage_service.check_agent_limit, task.agent
            )
            if not limit_status["allowed"]:
                logger.warning(f"Task {task.id} blocked by agent limit: {task.agent}")
                task_type = _determine_task_type(task, trigger_type)
                await asyncio.to_thread(
                    task_store.create_run, run_id, task.id, task.agent,
                    trigger_type, trigger_source, final_prompt, task_type,
                    task.scope, task.created_by,
                )
                await asyncio.to_thread(
                    task_store.update_run, run_id,
                    status="limit_exceeded",
                    error_message="Agent usage limit exceeded",
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
    if attempt == 1 and task.id in _active_task_ids:
        logger.warning(
            f"Skipping duplicate concurrent run for task {task.id}: "
            f"run {_active_task_ids[task.id]} still active"
        )
        return _active_task_ids[task.id]
    _active_task_ids[task.id] = run_id

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
        _running_tasks[run_id] = t
        t.add_done_callback(lambda _: _running_tasks.pop(run_id, None))
    except Exception:
        # Launch failed before _run_task could release the reservation.
        if _active_task_ids.get(task.id) == run_id:
            _active_task_ids.pop(task.id, None)
        raise

    # One-time tasks: mark fired immediately
    if task.run_at or task.delay_seconds is not None:
        await asyncio.to_thread(task_store.set_dynamic_task_fired, task.id)

    return run_id


# Mirrors ws/dashboard.py's STALE_TURN_SECS (event silence that ARMS a
# liveness probe — not death by itself; see the Mode D incident note there).
_STALL_PROBE_SECS = 90.0
_WATCHDOG_SLICE_S = 60.0


class _TaskTurnStalled(RuntimeError):
    """A headless task turn was reaped by the stall watchdog."""


async def _watch_task_pump(layer, pump, run_id: str, chat_id: str,
                           session_id: str) -> None:
    """Await the task pump with a stall watchdog.

    A headless turn had no wall-clock backstop: ``await pump._task`` waits for
    PRODUCER_DONE forever, and the wedge reap in ws/dashboard_chat.py only
    runs when a user re-opens the chat — an unwatched lane could sit
    "generating" for hours (observed: a lane stuck 1.5h+ in a never-ending
    tool call). Same reap criteria as the dashboard's: a severed remote
    stream, a silent-AND-dead process, or silence past the CLI turn ceiling.
    An alive process below the ceiling always gets its leash.
    """
    while True:
        try:
            await asyncio.wait_for(asyncio.shield(pump._task),
                                   timeout=_WATCHDOG_SLICE_S)
            return
        except asyncio.TimeoutError:
            pass
        if pump.producer.done():
            continue  # turn tail persisting — the pump is about to exit
        severed = layer.remote_stream_severed(session_id)
        idle = layer.session_idle_seconds(session_id)
        stale = idle is not None and idle > _STALL_PROBE_SECS
        hard_stale = idle is not None and idle > config.CLAUDE_TIMEOUT
        proc_dead = False
        if stale and not severed and not hard_stale:
            proc_dead = await layer.probe_session_process_dead(session_id)
        if not (severed or (stale and proc_dead) or hard_stale):
            continue
        if severed:
            reason = "stream severed by a satellite reconnect"
        elif hard_stale:
            reason = (f"stalled: no stream events for {int(idle)}s, "
                      f"hard ceiling exceeded")
        else:
            reason = (f"stalled: no stream events for {int(idle or 0)}s, "
                      f"process dead")
        logger.warning(
            f"Task watchdog: reaping stalled turn run={run_id} chat={chat_id} "
            f"session={session_id[:8]} ({reason})"
        )
        pump.abort()
        if pump._task is not None:
            # Wait WITHOUT cancelling — the pump's finally persists the
            # partial turn; a laggard is simply left to finish.
            await asyncio.wait([pump._task], timeout=2.0)
        try:
            await layer.prepare_resume(session_id)
        except Exception:
            logger.exception(
                f"Task watchdog: prepare_resume failed run={run_id}"
            )
        raise _TaskTurnStalled(f"reaped by platform: {reason}")


async def _run_task(run_id: str, session_id: str, task: TaskDefinition, prompt: str,
                    trigger_type: str, trigger_source: str | None, attempt: int,
                    trigger_payload: dict | None = None) -> None:
    from core.session import session_state as _state
    from core.concurrency import task_slot
    from core.session.session_manager import get_execution_layer
    from core.config.task_config_builder import build_task_agent_config, resolve_task_identity
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
    _ident = resolve_task_identity(task.agent, task.scope, task.created_by)
    try:
        _slot_target = (await asyncio.to_thread(
            remote_store.resolve_execution_target, task.agent,
            _ident.creds_user_sub, _ident.role,
        ))[0]
    except Exception:
        _slot_target = "local"

    async with task_slot(session_id, target=_slot_target):
        await asyncio.to_thread(
            task_store.update_run, run_id,
            status="running",
            started_at=now_iso(),
        )
        start = time.monotonic()
        chat_id = task.target_chat_id or f"task-{run_id}"
        output_cursor = 0  # pre-run message cursor; set in step 1
        prompt_row_id = 0  # the driven prompt's own row; excluded from collection
        layer = None  # bound in step 2; guards the except cleanup if config build fails
        # Set when a graceful abort leaves the session warm (step 7): the
        # finally must then keep its session-index entry so the idle reaper
        # + /v1/session/current still see the live session.
        session_kept_warm = False
        # Set when the step-1 lane gate had to reap a wedged prior pump: the
        # round must then respawn, never reuse (see _settle_prior_lane).
        lane_reaped = False

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
                lane_reaped = await _settle_prior_lane(chat_id, run_id)
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
            from core.config.config_builder import is_hard_fail_target, extract_offline_machine
            from storage import remote_store
            if is_hard_fail_target(agent_cfg.execution_target):
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
                    await _await_lane_quiescence(chat_id)
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
                    reused_warm = await _try_reuse_warm_session(
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
                    if agent_cfg.subscription_id:
                        from storage import subscription_store
                        subscription_store.decrement_active_sessions(
                            agent_cfg.subscription_id,
                        )
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

            # 4. Mark as task session so /v1/session/current excludes it.
            _state._sessions.setdefault(session_id, {"created": True, "message_count": 0})
            _state._sessions[session_id].update({
                "is_task": True,
                "client_type": "task",
                "agent": task.agent,
                "last_active": now_iso(),
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
                await _run_interactive_task(
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
                await _watch_task_pump(layer, pump, run_id, chat_id, session_id)

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
            final_status, session_kept_warm = _post_run_session_action(chat_row)
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
                final_output = mod_turns[-1]["content"] if mod_turns else _collect_task_output(chat_id, output_cursor)
            else:
                final_output = _collect_task_output(chat_id, output_cursor)

            # A run killed by a provider usage limit streams the limit notice
            # as a clean result text — without this check it lands as a
            # deceptive `completed` with the notice as output.
            limit_line = (
                _limit_notice(final_output) if final_status == "completed" else None
            )
            if limit_line:
                final_status = "failed"
                logger.warning(
                    f"Task run {run_id} stopped on a provider usage limit: {limit_line}"
                )

            duration_ms = int((time.monotonic() - start) * 1000)
            await asyncio.to_thread(
                task_store.update_run, run_id,
                status="failed" if limit_line else "completed",
                error_message=(
                    f"Provider usage limit: {limit_line}" if limit_line else None
                ),
                output_text=final_output[:10000] if final_output else "",
                completed_at=now_iso(),
                duration_ms=duration_ms,
                session_id=session_id,
                cost_usd=task_total_cost if task_total_cost > 0 else None,
                chat_id=chat_id,
            )
            logger.info(
                f"Task completed: run={run_id}, task={task.id}, duration={duration_ms}ms"
            )
            await _deliver_task_result(
                task, final_status, final_output,
                worker_chat_id=chat_id, output_cursor=output_cursor,
                prompt_row_id=prompt_row_id, prompt_text=prompt,
            )

            # Fire completion notification ONLY for 'auto' mode. 'manual' agents
            # fire their own (system-injected prompt tells them to); 'none' is
            # fully silent. A usage-limited run warns instead for every mode but
            # 'none' — the agent that would have self-notified in 'manual' died
            # with the limit.
            if limit_line:
                if task.notification_mode != "none":
                    from services.notifications import notification_manager
                    asyncio.create_task(notification_manager.fire_notification(
                        title=f"Task stopped: usage limit — {task.name}",
                        body=limit_line[:200],
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
            await _close_interactive_task_session(session_id)

        except asyncio.CancelledError:
            user_stop = run_id in _user_cancelled_runs
            platform_reason = _platform_interrupts.pop(run_id, None)
            # Graceful shutdown of a re-adoptable remote turn: LEAVE the run
            # running and DON'T close the satellite session — the CLI keeps
            # working, and after restart defer_orphaned_runs parks it +
            # sessions_alive re-adopts it (Mode C). Suppress the pump's
            # durable flush so the recovery replay doesn't duplicate content.
            from services.scheduler import run_recovery
            if (_shutting_down and not user_stop and platform_reason is None
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
                    completed_at=now_iso(),
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
                        "Proxy shutting down" if _shutting_down
                        else "Interrupted by platform"
                    ),
                    completed_at=now_iso(),
                    duration_ms=int((time.monotonic() - start) * 1000),
                )
            await _close_interactive_task_session(session_id)
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
            if task.on_complete_agent and not _shutting_down:
                if user_stop:
                    await _deliver_task_result(
                        task, "user_interrupted",
                        f'⚠ Delegated task "{task.name}" was stopped by the user '
                        f'before it finished.',
                        worker_chat_id=chat_id, output_cursor=output_cursor,
                        prompt_row_id=prompt_row_id, prompt_text=prompt,
                    )
                else:
                    await _deliver_task_result(
                        task, "cancelled",
                        f'⚠ Delegated task "{task.name}" was canceled before it finished.',
                    )

        except Exception as e:
            # A dead PTY on a watched interactive worker = the user closed or
            # killed the CLI deliberately — report user_interrupted, don't retry,
            # don't page anyone about a "failure".
            user_stop = isinstance(e, _InteractiveSessionDied) and e.had_viewer
            logger.error(f"Task failed: run={run_id}, task={task.id}: {e}", exc_info=True)
            await asyncio.to_thread(
                task_store.update_run, run_id,
                status="cancelled" if user_stop else "failed",
                error_message="Interrupted by user" if user_stop else str(e),
                completed_at=now_iso(),
                duration_ms=int((time.monotonic() - start) * 1000),
                chat_id=chat_id,
            )
            await _close_interactive_task_session(session_id)
            if layer is not None:
                await layer.close_session(session_id)

            # Collect whatever output was saved before the error; if none (a pre-turn
            # failure — bad config / dead process / offline target), deliver a
            # clearly-marked NON-EMPTY terminal so the delegating agent sees the
            # failure (not an empty bubble) and doesn't re-delegate. Delegate-only via
            # _deliver_task_result's on_complete gate; harmless for plain tasks.
            fail_output = _collect_task_output(chat_id, output_cursor) or (
                f'⚠ Delegated task "{task.name}" was stopped by the user.'
                if user_stop else f'⚠ Delegated task "{task.name}" failed: {e}'
            )
            await _deliver_task_result(
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
            _run_subscribers.pop(run_id, None)
            _run_event_buffer.pop(run_id, None)
            _user_cancelled_runs.discard(run_id)
            _platform_interrupts.pop(run_id, None)
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
            if _active_task_ids.get(task.id) == run_id:
                _active_task_ids.pop(task.id, None)
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
                    _dynamic_task_ids.discard(task.id)
                    logger.debug(f"Cleaned up fired one-time task: {task.id}")
                except Exception:
                    pass  # non-critical, task was already marked fired


async def _broadcast(run_id: str, event: dict) -> None:
    # Buffer event for late-subscriber replay (page refresh during active run)
    _run_event_buffer.setdefault(run_id, []).append(event)
    for q in list(_run_subscribers.get(run_id, [])):
        try:
            await q.put(event)
        except Exception as e:
            logger.debug("run event broadcast: %s", e)
