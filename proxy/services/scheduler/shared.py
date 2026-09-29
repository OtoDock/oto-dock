"""What every scheduler piece shares: the task models (``TaskDefinition``,
``RetryPolicy``), the in-memory registries of running and active runs, the
shutdown flag and ``now_iso``. Registries are mutated in place and may be
imported by name; ``_shutting_down`` is rebound and is read as
``shared._shutting_down``.

One piece of the task scheduler; ``services/scheduler/scheduler.py`` is the
facade that assembles them and holds the registry API. Every ``core.*`` and
service import stays function-local so the standalone scheduler can import
this package without the platform.
"""

import asyncio
import logging
from datetime import datetime, timezone

from pydantic import BaseModel


logger = logging.getLogger("claude-proxy.scheduler")


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
    # continuation authority can trace it. In-memory only.
    parent_chat_id: str | None = None
    project_id: str | None = None
    # Per-lane execution overrides — validated against the agent's envelope on
    # WRITE (delegate spawn, or task create/edit) via
    # ``spawn_authz.validate_spawn_overrides``. None / "" = inherit the
    # agent's default at fire time.
    #
    # model + execution_path PERSIST on ``dynamic_tasks`` (2026-08-16): a
    # schedule fires long after its creating session is gone, so the pin has
    # to survive a proxy restart. ``override_execution_mode`` stays in-memory
    # only — it picks interactive-vs-headless for a delegate worker somebody
    # is watching right now, which means nothing for an unattended run.
    override_model: str | None = None
    override_execution_path: str | None = None
    override_execution_mode: str | None = None
    # Bounded recurring continuations (task_type='continuation'): hard fire
    # bound and/or stop time — a chat must never wake itself forever.
    max_runs: int | None = None
    until_at: str | None = None
    # An app handler's schedule (task_type='app', APPS.md "Handlers"): the
    # app row and the handler; the runner writes a delivery, never a session.
    app_id: str | None = None
    app_handler: str | None = None
    # Checks (CHECKS.md): the refs attached to this task's runs (persisted
    # on dynamic_tasks.checks; copied onto each run's chat row at start).
    checks: list[str] = []
    # The first creator of a row the offboarding transfer moved (the
    # prompt's author); "" for a row that never changed hands. A transferred
    # row runs without the knowledge-write grant until a manager adopts it.
    transferred_from: str = ""
    # A check's judge run (task_type='check', in-memory only): the judge
    # profile — ``{"check": name, "mcps": [...], "judge_on": auto|platform,
    # "for_chat": the judged chat}``. A run with it is read-only, gets only
    # the listed MCPs, never runs interactive and is never judged itself.
    judge: dict | None = None
    # Where the run goes when the caller already knows (a judge mirrors the
    # judged session's machine; ``judge_on: platform`` pins "local"). Empty
    # = resolve_execution_target as usual.
    execution_target_override: str = ""
