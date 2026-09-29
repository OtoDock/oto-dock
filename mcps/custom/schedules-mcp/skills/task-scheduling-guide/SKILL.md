---
name: task-scheduling-guide
description: Parameter shapes and worked detail for the schedules-mcp tools — notification modes, cron and interval schedules, self-continuations, editing and reading back tasks, model pinning, trigger-only tasks, timezone rules. Load before creating or editing a scheduled task.
---

# Task scheduling — the guide

The `task-scheduling` card in your instructions carries the rules (when a
task is right, scope, notification mode, timing, timezone). This guide is
the reference behind them.

## Task completion notifications (`notification_mode`)

Every task requires a `notification_mode` — pick one when calling
`create_scheduled_task` or `create_one_time_task`. The system enforces this:
there is no default. The chosen mode controls both the system's behaviour AND
the task agent's behaviour, so they can never disagree.

| Mode | What the user gets on success | What the user gets on failure | When to pick |
|---|---|---|---|
| `auto` | Generic `"Task Complete: <name>"` (severity from `notify_severity`) | `"Task Failed: <name>"` (warning) | Status tasks where the user only cares that it finished — deploys, "wake me when X is done", sync confirmations. |
| `manual` | A custom notification fired by the task agent itself, with actual results | `"Task Failed: <name>"` (system safety net — crashed agents can't notify themselves) | Notification content matters — daily PR review (with findings), draft Reddit comments (with the drafts), email triage (with summary). Most user-facing tasks fall here. |
| `none` | Nothing | Nothing (user explicitly opted out) | High-frequency ops tasks where notifications would be spam — cache refresh, log rotation, vector store rebuild. User checks the task runs page for status. |

You do not need to write notification instructions in the task prompt. The
system auto-injects the right behaviour for the chosen mode into the task
agent's system prompt:

- Don't write: *"When done, send a notification to the user with create_notification."* — `manual` mode injects this for you.
- Don't write: *"A notification will be sent automatically — do not notify yourself."* — `auto` mode injects this for you.
- Just pick `notification_mode` and write the task prompt about what the agent should DO.

`notify_severity` controls the generic notification's severity in `auto`
mode (`info` / `success` / `warning`) and is ignored in `manual` and `none`.

## Scheduled self-continuations (`schedule_continuation`)

`schedule_continuation` wakes THIS session at a future time: the prompt you
give it is delivered into this very conversation as a new turn, with full
context (the session resumes automatically if it went idle). It works from
any session — chats and task runs alike.

- **Watchdog wake**: after delegating lanes, `schedule_continuation(prompt="Check on the delegated lanes — peek any that have not reported back.", in_seconds=3600)`. Cancel-on-arrival: if the thing you were watching for already happened (the callbacks arrived), `delete_task` the pending wake — don't let a stale watchdog fire.
- **Deferred round**: `schedule_continuation(prompt="Start phase 2 now if phase 1 finished.", at="2026-07-08T15:00:00")`.

Guardrails (enforced):

- Recurring continuations are always bounded — `max_runs` (default 5) or `until`. A chat must never wake itself forever. For indefinite monitoring, create a recurring **task** instead (`create_scheduled_task`) — each run gets a fresh context instead of accreting into this chat.
- Wakes **coalesce**: a new wake is skipped while a previous one is still unprocessed in this chat.
- Continuations auto-cancel when this chat is deleted, appear in `list_tasks`, and are cancelled with `delete_task`.

## Cron schedules

`create_scheduled_task` accepts standard 5-field POSIX cron — `minute hour day month weekday`. Sub-daily intervals are fully supported:

- `*/10 * * * *` — every 10 minutes
- `0 */3 * * *` — every 3 hours (top of the hour)
- `*/15 9-17 * * 1-5` — every 15 minutes, 9am-5pm, weekdays
- `0 9 * * 1-5` — weekdays at 9am
- `0 0 * * 0` — every Sunday at midnight

Pick the coarsest interval that meets the user's intent — don't schedule
`* * * * *` (every minute) unless they explicitly need that. For cadences
that don't divide 24 evenly (every 17 hours, every 5h30m, every 3 days), use
`interval_seconds` instead of cron.

## Managing existing tasks

- **`list_tasks`** — shows all tasks for this agent with their current status (`active` or `paused`), schedule, next run time, and ID. Always call this first to find the task ID before pausing/resuming/deleting.
- **`get_task(task_id)`** — one task's full definition: the prompt verbatim, when it fires and in which zone (a trigger-type task lists the triggers pointing at it, or says none are wired yet), status and next run, timeout, notification mode, what it runs on (model with its tier, pinned or default, and the layer when one is pinned), warnings when a pin no longer resolves (a disabled model, an engine the agent no longer has), run count and limits. It is the only read that shows a task's prompt; `get_task_history` lists a task's runs and `get_task_result` the latest run's output. Read-only. Task ids are global: another accessible agent's task reads with its id alone.
- **Cross-agent reads**: `list_tasks`, `get_task_history`, and `get_task_result` take an optional `agent` argument — another agent's slug, or `"all"` for every agent your user can access; `get_task` takes none, since a task id already names its agent. You see exactly what your user would see on that agent's pages (their role travels with you); sessions without a user see only agent-scope tasks and runs, of this agent and its wired delegation targets (the scheduled-briefing pattern: read what your delegation targets did, then summarize). Reading is where it ends: to CHANGE another agent's schedule, ask that agent via delegation — mutations never cross agents.
- **`pause_task(task_id)`** — stops a task from firing on its schedule without deleting it. The task can be resumed later. Use when the user says "pause", "stop for now", "disable temporarily".
- **`resume_task(task_id)`** — re-enables a paused task. For one-time tasks whose `run_at` has already passed, resume keeps the row active but does NOT auto-fire — the user can run it manually from the dashboard if they want.
- **`edit_task(task_id, ...)`** — change the schedule, run time, name, prompt, timeout, notification settings, the timezone (`timezone=`, see below), or the model / execution layer of an existing task **without deleting and recreating it**. Pass only the fields you want to change. `schedule`, `interval_seconds` and `run_at` are mutually exclusive — setting one switches the mode and clears the others. Use this whenever the user says "change the time of X", "update the X reminder to Y", "make this run every 3 hours instead", "edit the prompt of X". Always prefer this over delete+recreate — it preserves the task ID, history references, and any in-flight context.
- **`delete_task(task_id)`** — permanently removes a task (or a pending continuation). Use when the user says "delete", "remove", "cancel for good". Static tasks (defined in `tasks.json`) cannot be deleted via this tool.
- **`run_task(task_id, wait?, timeout_seconds?)`** — fires an existing task immediately, regardless of schedule. Use for "run X now" or to manually fire a paused/expired task. With `wait=true` the call returns the run's output once the run ends, with its status (completed, failed or cancelled); after `timeout_seconds` (default 600) it returns while the run continues, so read the result later with `get_task_result`.
- **`cancel_task_run(run_id)`** — stops a running or queued run (the id from `get_task_history`); a `run_task` wait on that run returns at once with status cancelled.
- **`get_task_history(task_id?, limit?)`** — recent runs: id, status, started, duration, the error when one failed. `Left running: N …` on a run means it ended with `run_in_background` commands or subagents still running: the run waited for them up to the task's `timeout_seconds` (never less than 600 s), then completed without their output; the session stays alive for them up to a platform ceiling. Give a long pipeline the `timeout_seconds` it needs.
- **`get_task_result(task_id)`** — the latest run's status and output.
- An `[app]` row in `list_tasks` is an app handler the platform wakes on a schedule: no LLM turn, no `runs on:`, and `model` / `layer` do not apply to it.

One-time tasks auto-clean up after they fire successfully — the row is
removed and won't appear in `list_tasks`. Recurring tasks persist and keep
firing until paused or deleted.

## Choosing a model for a task

Every task runs on **your agent's default model and execution layer** unless
it says otherwise. That default is the right answer almost always — leave
`model` and `layer` out.

The exception is a task whose cost profile genuinely differs from your
everyday work: a heavy daily briefing that has to reason over a lot of
material, or conversely a trivial hourly sync that would be wasteful on a big
model. `create_scheduled_task`, `create_one_time_task`, and `edit_task` all
take an optional `model` (and `layer`) that pins **that one task's runs**,
leaving the agent's default untouched for everything else. It is a real cost
lever: a lighter default with one task pinned up, or a frontier default with
the noisy tasks pinned down.

- **Ask the user before pinning a model, unless they already asked for it.** Which model a schedule burns is their spend, not your call. "Should I run that daily report on <stronger model> and keep everything else on the default?" is the shape of the question.
- **Valid values come from your own `layers:` line** — it lists your enabled execution layers and the model ids on each, with your current default marked `[default, tN]`. Look for it under **Scheduled-task Execution** in your prompt; if you also have the delegation tools it lives on your own row in **Available Agents** instead, and the Scheduled-task Execution block points you there. A model that isn't on that line is rejected with a 400; do not guess ids.
- **Pick by tier, not by name.** Every id on that line carries a capability tag, `[t1]` (frontier: complex coding, the hardest reasoning, judgement calls, long autonomous work) down to `[t4]` (fast: mechanical, high-volume routine work); `[t?]` is a local or custom model nobody rated. The **Model tiers** list next to it says what each model is good at. Complex, open-ended or judgement-heavy tasks (a code change on a real codebase, a review, a decision, a briefing that weighs evidence) belong on tier 1; only a mechanical, well-defined job (a sync, a reformat) belongs on tier 3 or 4. A newer, bigger-sounding or more expensive-sounding id is not a stronger model: the tag is.
- **Retuning is cheap.** `edit_task(task_id, model="...")` re-pins a live schedule from its next run on, with no change to when it fires. `edit_task(task_id, model="")` clears the pin and returns the task to your default.

## Checks on a task (`checks`)

`create_scheduled_task`, `create_one_time_task` and `edit_task` take an
optional `checks`: the names of checks (the `checks` skill — named units that
judge work at the end of a turn) to attach to every run of the task. The
run's turn is judged when it ends; the agent gets the findings and fixes
them within the check's fix rounds, and a verdict that still does not pass
fails the run with the findings as its error. Name the agent's offered checks
by name and your own by `user:<name>`; the agent's mandatory checks run on
every task whether or not you name them. `edit_task(task_id, checks=[])`
clears the list.
- **Read it back, never assume.** `list_tasks` shows every task's effective model, its tier and layer — `runs on: <model> [pinned, tier 2 strong]` for a task-level pin, `[agent default, …]` when it follows the agent's current setting, `[layer default, …]` when only the engine is pinned and that engine's own first choice runs. `get_task` shows the same plus the prompt and any warning about a pin that no longer resolves. When asked "what model does this task run on?", answer from these reads, not from memory or the default. The dashboard's Scheduled Tasks tab shows the same.

## Trigger-only tasks (`task_type='trigger'`)

A task can also be paired with a **webhook trigger** instead of a schedule.
Use `create_one_time_task(name=..., prompt=..., task_type='trigger')` — the
task is stored without `run_at` or `delay_seconds`, and only runs when an
external system calls the trigger's webhook URL. This pattern is for personal
automations and business event handlers:

- "Run my code-review task whenever GitHub fires the PR-opened webhook"
- "Run the deploy-checker agent task whenever the CI webhook fires"

The task prompt can use `{{placeholder}}` tokens that get substituted from
the webhook body at fire time. Wire it up afterwards with
`create_trigger(task_id=...)` from the **triggers-mcp**. Cross-scope linkage
is enforced — a user-scoped trigger can only run a user-scoped task;
agent-scoped triggers only agent-scoped tasks.

## Timezone semantics

The `[Current time: ...]` line at the start of each user message shows the
user's **local** timezone with the IANA name and explicit UTC offset (e.g.
`Europe/Athens (UTC+03:00)`, `America/New_York (UTC-04:00)`). The time is
rendered in 24-hour form first, then in parentheses again as 12-hour with
AM/PM (e.g. `04:02 (4:02 AM)`, `17:00 (5:00 PM)`) — always trust the AM/PM
gloss; never guess the half-of-day from the 24-hour number alone. This is
detected from the user's browser, so it follows them when they travel.

When you compute future times for `run_at` (or `schedule_continuation`'s `at`):

- **Prefer naive ISO** (no offset, e.g. `'2026-04-29T10:00:00'`). The proxy interprets it in the user's local timezone — same one you see in `[Current time: ...]`. This matches how users speak ("remind me at 10am") and travels with them.
- **Don't append `Z` or `+00:00`** unless you genuinely mean UTC. UTC-tagged times are stored at the literal absolute moment, which is rarely what the user meant when they said a wall-clock time.
- **Recurring `schedule` (cron)** is also evaluated in the user's local timezone, snapshotted on the row at creation. A new `schedule` alone keeps the row's zone; to change the zone of a recurring task (e.g. the user moved permanently), pass `edit_task(task_id, timezone="Asia/Tokyo")`.
- **`timezone` (create and edit) is for a zone the person NAMED.** Omit it and the task follows the zone of the person you are talking to when there is one, else the platform zone — and keeps following it if an admin changes that setting. In a task or trigger session the `[Current time: ...]` line shows the PLATFORM zone: never pass that as a guess, it pins a literal that stops following the setting. `get_task` shows which zone a task fires in.
