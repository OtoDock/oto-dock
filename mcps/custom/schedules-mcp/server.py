"""Schedules MCP Server.

stdio MCP server for scheduled, one-time, and trigger-fired background tasks,
plus scheduled self-continuations of the calling session. Communicates with
the proxy's Task Management REST API. Delegation (parallel worker sessions)
lives in the separate delegation-mcp.

Environment variables (set in per-agent mcp-config.json):
  SCHEDULES_MCP_AGENT      - Which agent this instance serves (e.g. "system-admin")
  PROXY_URL                - Proxy URL (e.g. "http://localhost:8400")
  SCHEDULES_MCP_API_KEY    - Proxy API key (falls back to PROXY_API_KEY from process env)

Cross-agent reads: the read tools take an optional ``agent`` arg — a slug,
or "all" for every agent the session's user can access. Server-side the
proxy filters by the TOKEN identity (reads follow the user), so this server
just forwards the arg: "all" maps to omitting the ``agent`` query param.
No-user sessions reach only their own agent plus their wired delegation
targets (agent-scope rows, read-only) — also proxy-enforced.
"""

import asyncio
import json
import os

import httpx
from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import TextContent, Tool

AGENT = os.environ.get("SCHEDULES_MCP_AGENT", "system-admin")
PROXY_URL = os.environ.get("PROXY_URL", "http://localhost:8400").rstrip("/")
API_KEY = os.environ.get("SCHEDULES_MCP_API_KEY") or os.environ.get("PROXY_API_KEY", "")
# This tool's own wait outcome for ``run_task(wait=true)``: the stream was
# still open when the wait gave up listening. Not a run status — the run
# keeps going and its real status is read later with get_task_result.
WAIT_TIMED_OUT = "timeout"


# The shared `agent` arg of the read tools (list_tasks / get_task_history /
# get_task_result). Visibility is enforced server-side from the session token.
_AGENT_ARG_SCHEMA = {
    "type": "string",
    "description": (
        "Cross-agent read: an agent slug, or 'all' for every agent your "
        f"user can access. Omit for this agent ({AGENT}). Sessions without "
        "a user (scheduled agent-scope runs) see only this agent plus its "
        "wired delegation targets (agent-scope, read-only)."
    ),
}


# The shared per-task execution pins of the write tools (create_scheduled_task
# / create_one_time_task / edit_task). Validated server-side against this
# agent's envelope — the same check delegate() gets — so a bad value is a
# clear error, never a silent downgrade.
_MODEL_ARG_SCHEMA = {
    "type": "string",
    "description": (
        "OPTIONAL model override for THIS task's runs — every run uses it "
        f"instead of {AGENT}'s default model. Use it to pin one demanding "
        "task (a daily briefing, a report) to a stronger model while the "
        "agent's everyday default stays lighter, or the reverse. Must be a "
        "model enabled on the task's execution layer — read the valid ids "
        "off this agent's `layers:` line in the prompt, and pick by the "
        "capability tier tagged there ([t1] frontier … [t4] fast; the Model "
        "tiers list explains each), never by how an id sounds. Omit to "
        "inherit the agent's default (recommended). On `edit_task`, pass "
        "\"\" to clear an existing pin."
    ),
}

_LAYER_ARG_SCHEMA = {
    "type": "string",
    "description": (
        "OPTIONAL execution-layer override for THIS task's runs (e.g. "
        "'claude-code-cli', 'codex-cli', 'direct-llm') — must be enabled for "
        f"{AGENT}. Omit to inherit the agent's default layer. On `edit_task`, "
        "pass \"\" to clear an existing pin."
    ),
}

_CHECKS_ARG_SCHEMA = {
    "type": "array",
    "items": {"type": "string"},
    "description": (
        "OPTIONAL checks to run at the end of each run (the agent's offered "
        "checks or your own, by name — the checks tool's list_checks names "
        "them). A failing check hands the run its findings for another round "
        "before the run ends; the agent's mandatory checks run regardless. On "
        "`edit_task`, pass the whole list ([] clears it)."
    ),
}

# The zone a schedule and a naive run_at are read in. The trap this wording
# guards against: an agent in a task or trigger session sees the PLATFORM
# zone in its [Current time] line and passes it "to be safe" — which pins a
# literal that stops following the platform timezone setting, while an
# omitted zone keeps following it (the row stays NULL for such sessions).
_TIMEZONE_ARG_SCHEMA = {
    "type": "string",
    "description": (
        "OPTIONAL IANA zone the schedule and run_at are read in (e.g. "
        "'Europe/Athens'). Omit `timezone`: the task follows the zone of the "
        "person you are talking to when there is one, else the platform "
        "zone, and keeps following it. Pass it only when the person asked "
        "for a schedule in another named zone — never the zone from the "
        "[Current time] line as a guess."
    ),
}


def _fires_info(t: dict) -> str:
    """When a task fires, in words: cron, run_at, interval, delay, a
    trigger-type task's triggers, or an app handler's schedule."""
    if t.get("task_type") == "trigger":
        trig = t.get("triggers")
        if trig is None:
            return "on trigger"
        return f"on trigger ({len(trig)} wired)" if trig else "on trigger (none wired yet)"
    return (
        t.get("schedule")
        or t.get("run_at")
        or (f"every {t['interval_seconds']}s" if t.get("interval_seconds") else None)
        or (f"delay {t['delay_seconds']}s" if t.get("delay_seconds") is not None else None)
        or "one-time"
    )


def _runs_on(t: dict) -> str:
    """What the task's runs execute on, as a ', runs on: …' suffix. The
    backend resolves effective_model / effective_execution_path (the task's
    pin, else the agent's CURRENT default resolved with the effective
    layer) and tags the source; the model's capability tier rides along.
    An unresolved default comes back "" — render nothing rather than a
    wrong claim (matches the dashboard). An app handler row runs no LLM
    turn at all, so it never claims a model."""
    if t.get("task_type") == "app":
        return ""
    eff_model = t.get("effective_model") or ""
    source = t.get("effective_model_source") or (
        "pinned" if t.get("override_model") else "agent default")
    tier = t.get("effective_model_tier")
    tier_bit = f", tier {tier} {t.get('tier_label') or ''}".rstrip() if tier else ", untiered"
    layer_bit = (
        f" via {t['effective_execution_path']} [pinned layer]"
        if t.get("override_execution_path")
           and t.get("effective_execution_path") else ""
    )
    if eff_model:
        return f", runs on: {eff_model} [{source}{tier_bit}]{layer_bit}"
    return layer_bit and f", runs{layer_bit}"


def _lane_line(args: dict, cleared: bool = False) -> str:
    """One confirmation line for the per-task model/layer pins, or ''.

    Echoing the pin back matters: the agent needs to see that the task is NOT
    on the agent default any more. ``cleared`` (edit path) also reports an
    explicit "" as a reset rather than staying silent about it.
    """
    bits = []
    for key in ("model", "layer"):
        if key not in args:
            continue
        if args[key]:
            bits.append(f"{key}={args[key]}")
        elif cleared:
            bits.append(f"{key}=agent default (pin cleared)")
    return f"Runs on: {', '.join(bits)}" if bits else ""


def _agent_param(args: dict) -> dict:
    """Resolve the ``agent`` arg into query params: default = own agent,
    'all' = no filter (the proxy then returns every accessible agent)."""
    agent = args.get("agent") or AGENT
    return {} if agent == "all" else {"agent": agent}

# Per-agent default scope (drives the scope arg default for every
# scope-aware MCP). Falls back through PROXY_TASK_SCOPE (the session's
# actual task scope) and OTO_SCOPE (any session) before settling on the
# safe "user" default.
DEFAULT_SCOPE = (
    os.environ.get("OTO_DEFAULT_SCOPE")
    or os.environ.get("PROXY_TASK_SCOPE")
    or os.environ.get("OTO_SCOPE")
    or "user"
)

# visibility-modes: the agent's mode scopes (Personal-only → ["user"], Shared-only
# → ["agent"], collaborative → both). Filters the scope arg's enum so the LLM
# never picks a scope this agent doesn't have. Unset → both (legacy/pre-modes).
# The API re-checks server-side (defense in depth).
AVAILABLE_SCOPES = [
    s for s in (os.environ.get("OTO_AVAILABLE_SCOPES", "") or "").split(":")
    if s in ("user", "agent")
] or ["user", "agent"]
# The single scope fallback for BOTH the advertised schema default and the
# create handlers. They must agree: a shared-only agent's schema says
# `agent`, so a handler defaulting to a literal "user" made every
# scope-omitted create 400 ("does not support 'user'-scoped tasks").
SCOPE_DEFAULT = DEFAULT_SCOPE if DEFAULT_SCOPE in AVAILABLE_SCOPES else AVAILABLE_SCOPES[0]


server = Server("schedules-mcp")


def _headers() -> dict:
    return {
        "Authorization": f"Bearer {API_KEY}",
        "X-Agent-Name": AGENT,
        "Content-Type": "application/json",
    }


async def _post(path: str, body: dict, headers: dict | None = None) -> dict:
    async with httpx.AsyncClient(timeout=30.0) as client:
        resp = await client.post(f"{PROXY_URL}{path}", json=body, headers=headers or _headers())
        resp.raise_for_status()
        return resp.json()


async def _get(path: str, params: dict | None = None) -> dict:
    async with httpx.AsyncClient(timeout=30.0) as client:
        resp = await client.get(f"{PROXY_URL}{path}", params=params, headers=_headers())
        resp.raise_for_status()
        return resp.json()


async def _delete(path: str, headers: dict | None = None) -> dict:
    async with httpx.AsyncClient(timeout=30.0) as client:
        resp = await client.delete(f"{PROXY_URL}{path}", headers=headers or _headers())
        resp.raise_for_status()
        return resp.json()


@server.list_tools()
async def list_tools() -> list[Tool]:
    return [
        Tool(
            name="create_scheduled_task",
            description=(
                "Create a recurring task — fire-and-forget only. Read the skill "
                "`task-scheduling-guide` (Skill tool) before creating a recurring task: "
                "notification modes, cron vs interval, model pinning, timezone rules. "
                "Returns immediately; the task runs on its own schedule in the background. "
                "No result is returned to this session — use get_task_history to check past runs. "
                "Use this only for genuinely recurring automation (daily reports, weekly checks, etc.). "
                "If you need the result of background work, use the delegation tools instead.\n\n"
                "Provide EXACTLY ONE of `schedule` (cron) or `interval_seconds` (fixed real-time interval). "
                "Use `schedule` for fixed wall-clock times (weekdays at 9am, every Monday). "
                "Use `interval_seconds` for fixed real-time intervals where the cadence does NOT divide 24 cleanly "
                "(every 17 hours, every 5h30m, every 3 days). CRITICAL: `0 */N * * *` in cron only works "
                "when N evenly divides 24 (e.g. 1, 2, 3, 4, 6, 8, 12). For 5, 7, 17, etc., cron will fire "
                "at hours 0 + N + 2N within a day and reset at midnight — NOT every N hours. Always prefer "
                "`interval_seconds` for those cases."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "name": {"type": "string", "description": "Short human-readable name"},
                    "prompt": {"type": "string", "description": "The prompt to execute each run"},
                    "schedule": {
                        "type": "string",
                        "description": (
                            "Standard 5-field POSIX cron: 'minute hour day month weekday'. "
                            "Use for WALL-CLOCK schedules. Examples: '0 9 * * 1-5' (weekdays at 9am), "
                            "'*/10 * * * *' (every 10 minutes), "
                            "'0 */3 * * *' (every 3 hours — works because 3 divides 24), "
                            "'*/15 9-17 * * 1-5' (every 15 minutes during business hours, weekdays). "
                            "DO NOT use cron for intervals like 'every 17 hours' or 'every 5 hours' "
                            "that don't divide 24 evenly — use interval_seconds instead. "
                            "Mutually exclusive with interval_seconds."
                        ),
                    },
                    "interval_seconds": {
                        "type": "integer",
                        "minimum": 60,
                        "maximum": 31536000,
                        "description": (
                            "Fire every N seconds, anchored at task creation time. Use for "
                            "REAL-TIME intervals. Examples: 3600 (every hour), 61200 (every 17 hours), "
                            "19800 (every 5h30m), 259200 (every 3 days), 604800 (every week from now). "
                            "Min 60s, max 31536000s (1 year). Mutually exclusive with schedule. "
                            "First fire is exactly one interval after creation, never on creation itself."
                        ),
                    },
                    "llm_mode": {
                        "type": "string",
                        "enum": ["cli", "direct"],
                        "description": "Execution mode (cli=default)",
                        "default": "cli",
                    },
                    "timeout_seconds": {
                        "type": "integer",
                        "description": "Max execution time per run in seconds",
                        "default": 600,
                    },
                    "scope": {
                        "type": "string",
                        "enum": AVAILABLE_SCOPES,
                        "default": SCOPE_DEFAULT,
                        "description": (
                            f"Default for this agent: `{DEFAULT_SCOPE}`. "
                            "'user' = owned by current user only; "
                            "'agent' = agent-wide task visible to all users (editor role or above). "
                            "Omit to use the agent's default; override only when the user explicitly "
                            "wants the other scope."
                        ),
                    },
                    "notification_mode": {
                        "type": "string",
                        "enum": ["auto", "manual", "none"],
                        "description": (
                            "How the user is notified when this task completes. "
                            "REQUIRED — no default.\n"
                            "- 'auto': system sends a generic 'Task Complete: <name>' "
                            "notification on success (severity from notify_severity) and "
                            "'Task Failed' on failure. Pick this for status tasks where "
                            "'done' is enough information.\n"
                            "- 'manual': the task agent fires its own notification with "
                            "actual results via create_notification. The system tells the "
                            "task agent to do so — you do NOT need to mention notifications "
                            "in the prompt. Pick this when the notification content matters "
                            "(research findings, drafts, summaries, alerts with context). "
                            "System still sends a failure notification if the agent crashes.\n"
                            "- 'none': fully silent. Pick this for high-frequency ops tasks "
                            "(cache refresh, log rotation, sync jobs) where notifications "
                            "would be spam. Silent on failure too — check the task runs page.\n"
                            "Do NOT add notification instructions to the task prompt yourself "
                            "— the system auto-injects the right behaviour based on this field."
                        ),
                    },
                    "notify_severity": {
                        "type": "string",
                        "enum": ["info", "success", "warning"],
                        "default": "info",
                        "description": "Severity for the generic 'Task Complete' notification (only used in 'auto' mode)",
                    },
                    "model": _MODEL_ARG_SCHEMA,
                    "layer": _LAYER_ARG_SCHEMA,
                    "checks": _CHECKS_ARG_SCHEMA,
                    "timezone": _TIMEZONE_ARG_SCHEMA,
                },
                "required": ["name", "prompt", "notification_mode"],
            },
        ),
        Tool(
            name="create_one_time_task",
            description=(
                "Schedule a one-time task to run at a specific future time or after a delay — "
                "fire-and-forget only. "
                "Returns immediately; no result is returned to this session. "
                "Use this when the user asks to schedule or delay something and doesn't need the result "
                "(e.g. 'send a reminder in 2 hours', 'run cleanup tonight'). "
                "If you need the result back in THIS conversation, use the delegation tools instead."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "name": {"type": "string", "description": "Short human-readable name"},
                    "prompt": {"type": "string", "description": "The prompt to execute"},
                    "run_at": {
                        "type": "string",
                        "description": (
                            "ISO datetime in the user's local timezone — the "
                            "one shown in the [Current time: ...] line of the "
                            "user message. Example: '2026-03-12T14:00:00'. "
                            "Prefer naive (no offset) when matching the user's "
                            "wall-clock intent — the proxy interprets it in "
                            "the user's local timezone automatically. If you "
                            "include an explicit offset like '+03:00' or 'Z', "
                            "it is respected exactly."
                        ),
                    },
                    "delay_seconds": {
                        "type": "integer",
                        "description": "Run after this many seconds from now",
                    },
                    "llm_mode": {
                        "type": "string",
                        "enum": ["cli", "direct"],
                        "default": "cli",
                    },
                    "timeout_seconds": {
                        "type": "integer",
                        "default": 600,
                    },
                    "scope": {
                        "type": "string",
                        "enum": AVAILABLE_SCOPES,
                        "default": SCOPE_DEFAULT,
                        "description": (
                            f"Default for this agent: `{DEFAULT_SCOPE}`. "
                            "'user' = owned by current user only; "
                            "'agent' = agent-wide task (editor role or above). "
                            "Omit to use the agent's default; override only when the user explicitly "
                            "wants the other scope."
                        ),
                    },
                    "task_type": {
                        "type": "string",
                        "enum": ["one_time", "trigger"],
                        "default": "one_time",
                        "description": (
                            "'one_time' (default) = runs once at run_at or after "
                            "delay_seconds. 'trigger' = a trigger-only task that fires "
                            "ONLY when a webhook trigger calls it. With 'trigger', "
                            "do not pass run_at / delay_seconds. Use this when the "
                            "user wants a reusable task to be wired up to a "
                            "webhook (GitHub, Stripe, etc.) via the triggers-mcp."
                        ),
                    },
                    "notification_mode": {
                        "type": "string",
                        "enum": ["auto", "manual", "none"],
                        "description": (
                            "How the user is notified when this task completes. "
                            "REQUIRED — no default. See create_scheduled_task for the full "
                            "auto/manual/none decision matrix. Do NOT add notification "
                            "instructions to the task prompt — the system auto-injects them."
                        ),
                    },
                    "notify_severity": {
                        "type": "string",
                        "enum": ["info", "success", "warning"],
                        "default": "info",
                        "description": "Severity for the generic 'Task Complete' notification (only used in 'auto' mode)",
                    },
                    "model": _MODEL_ARG_SCHEMA,
                    "layer": _LAYER_ARG_SCHEMA,
                    "checks": _CHECKS_ARG_SCHEMA,
                    "timezone": _TIMEZONE_ARG_SCHEMA,
                },
                "required": ["name", "prompt", "notification_mode"],
            },
        ),
        Tool(
            name="schedule_continuation",
            description=(
                "Schedule a future continuation of THIS session: at the given time, the "
                "prompt is delivered into this very conversation as a new turn (with full "
                "context — the session resumes if it has gone idle). Use it for watchdog "
                "wake-ups ('in 1 hour, check whether the delegated lanes reported back') "
                "and deferred rounds ('at 15:00, start phase 2 if phase 1 finished').\n\n"
                "One-shot: provide exactly one of `at` / `in_seconds`. Recurring: provide "
                "`repeat_cron` or `repeat_interval_seconds` — recurring continuations are "
                "ALWAYS bounded (max_runs, default 5, or an `until` time); a chat must "
                "never wake itself forever. For indefinite monitoring, create a recurring "
                "TASK instead (fresh context per run — no context accretion in this chat).\n\n"
                "Wake-ups COALESCE: a new wake is skipped while a previous one is still "
                "unprocessed in this chat. Pending continuations appear in list_tasks and "
                "are cancelled with delete_task (cancel yours when its purpose is served — "
                "e.g. a watchdog wake that arrives after the thing it watched for already "
                "happened should be deleted, not answered)."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "prompt": {
                        "type": "string",
                        "description": "The prompt delivered to this session when the continuation fires",
                    },
                    "at": {
                        "type": "string",
                        "description": (
                            "ISO datetime for a one-shot continuation (user-local naive time, "
                            "like run_at elsewhere). Mutually exclusive with in_seconds."
                        ),
                    },
                    "in_seconds": {
                        "type": "integer",
                        "minimum": 30,
                        "description": "Fire after N seconds from now (one-shot). Mutually exclusive with at.",
                    },
                    "repeat_cron": {
                        "type": "string",
                        "description": (
                            "5-field cron for a RECURRING continuation. Requires max_runs "
                            "or until. Mutually exclusive with repeat_interval_seconds/at/in_seconds."
                        ),
                    },
                    "repeat_interval_seconds": {
                        "type": "integer",
                        "minimum": 60,
                        "maximum": 31536000,
                        "description": (
                            "Recurring every N seconds. Requires max_runs or until. "
                            "Mutually exclusive with repeat_cron/at/in_seconds."
                        ),
                    },
                    "max_runs": {
                        "type": "integer",
                        "minimum": 1,
                        "maximum": 100,
                        "description": "Hard bound on recurring fires (default 5 when neither max_runs nor until given).",
                    },
                    "until": {
                        "type": "string",
                        "description": "ISO datetime after which a recurring continuation stops firing.",
                    },
                    "name": {
                        "type": "string",
                        "description": "Optional short label (defaults to a preview of the prompt).",
                    },
                },
                "required": ["prompt"],
            },
        ),
        Tool(
            name="run_task",
            description=(
                "Trigger an existing task by its ID and optionally wait for it to finish. "
                "Use this to manually run a static or dynamic task that already exists "
                "(e.g. the scheduled auto-update or health-check tasks). "
                "Call list_tasks first to find the task_id. "
                "Set wait=true to block and get the output inline (good for short tasks); "
                "the wait ends when the run ends with any status (completed, failed or "
                "cancelled) or when timeout_seconds passes. "
                "Set wait=false (default) to fire-and-forget."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "task_id": {"type": "string", "description": "ID of the existing task to run"},
                    "wait": {
                        "type": "boolean",
                        "description": (
                            "If true, block until the run ends (completed, failed or cancelled; "
                            "the status is reported with the output) or timeout_seconds passes, "
                            "and return the output. Default false."
                        ),
                        "default": False,
                    },
                    "timeout_seconds": {
                        "type": "integer",
                        "description": "Max wait time when wait=true (default 600)",
                        "default": 600,
                    },
                },
                "required": ["task_id"],
            },
        ),
        Tool(
            name="get_task_result",
            description=(
                "Get the latest run status and output for a task. "
                "Non-blocking — returns whatever is in the DB right now. "
                "Use after fire-and-forget tasks to check if they completed, "
                "or after a callback to inspect the full output."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "task_id": {"type": "string", "description": "Task ID to check"},
                    "agent": _AGENT_ARG_SCHEMA,
                },
                "required": ["task_id"],
            },
        ),
        Tool(
            name="list_tasks",
            description=(
                "List all tasks (static + agent-created) for this agent, including "
                "pending session continuations. Shows each task's schedule, next run "
                "time, status (active or paused), and what its runs execute on: "
                "`runs on: <model> [pinned|agent default|layer default, tier N "
                "<label>]`, plus `via <layer> [pinned layer]` when the engine is "
                "pinned (`layer default` = only the engine is pinned and its own "
                "first choice runs); an `[app]` handler row runs no LLM turn and "
                "shows no model. This is the authoritative read for "
                "\"what model does this task run on?\" — never assume the default. "
                "Use this to find a task before "
                "calling pause_task, resume_task, run_task, or delete_task. "
                "With `agent`, lists another accessible agent's tasks instead "
                "(read-only — mutations stay same-agent; ask that agent via "
                "delegation to change its schedule)."
            ),
            inputSchema={
                "type": "object",
                "properties": {"agent": _AGENT_ARG_SCHEMA},
            },
        ),
        Tool(
            name="get_task",
            description=(
                "Read ONE task's full definition: its prompt (verbatim), when "
                "it fires (schedule with timezone, run_at, interval, delay, "
                "or the triggers pointing at a trigger-type task), status and "
                "next run, timeout, notification mode, what its runs execute "
                "on (model with its capability tier, pinned or default, and "
                "the layer when one is pinned), warnings when a pin no longer resolves, "
                "run count and limits. The read for \"what does this task do "
                "and should it run on another model?\": list_tasks never "
                "shows the prompt and get_task_history only covers tasks that "
                "have run. Read-only; task ids are global, so another accessible "
                "agent's task reads with its id alone (same visibility as "
                "list_tasks)."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "task_id": {"type": "string", "description": "Task ID (from list_tasks, any accessible agent)"},
                },
                "required": ["task_id"],
            },
        ),
        Tool(
            name="delete_task",
            description=(
                "Delete an agent-created (dynamic) task or a pending session "
                "continuation. Returns an error if the task is a static task "
                "from tasks.json."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "task_id": {"type": "string", "description": "Task ID to delete"},
                },
                "required": ["task_id"],
            },
        ),
        Tool(
            name="pause_task",
            description=(
                "Pause a scheduled or one-time task without deleting it. "
                "The task stays in the system but won't fire on its schedule "
                "until resumed via resume_task. Static tasks (from tasks.json) "
                "cannot be paused."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "task_id": {"type": "string", "description": "Task ID to pause"},
                },
                "required": ["task_id"],
            },
        ),
        Tool(
            name="resume_task",
            description=(
                "Resume a paused task so it fires on its schedule again. "
                "For one-time tasks whose scheduled time has already passed, "
                "the task will not fire automatically — the user can run it "
                "manually from the dashboard if they want."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "task_id": {"type": "string", "description": "Task ID to resume"},
                },
                "required": ["task_id"],
            },
        ),
        Tool(
            name="edit_task",
            description=(
                "Edit a scheduled or one-time task in place — change its "
                "schedule, run time, name, prompt, notification settings, or "
                "the model / execution layer its runs use, without deleting "
                "and recreating it. Model and layer changes apply from the "
                "next run; the schedule keeps firing exactly as before. "
                "At least one editable field besides task_id must be provided. "
                "schedule, interval_seconds, and run_at are mutually exclusive: "
                "setting one switches the task's mode and automatically clears "
                "the others. Static tasks (defined in tasks.json) cannot be edited."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "task_id": {"type": "string", "description": "Task ID to edit"},
                    "name": {
                        "type": "string",
                        "description": "New display name (optional)",
                    },
                    "prompt": {
                        "type": "string",
                        "description": "New prompt to execute (optional)",
                    },
                    "schedule": {
                        "type": "string",
                        "description": (
                            "New cron expression for a recurring task. "
                            "Standard 5-field POSIX cron. Examples: "
                            "'*/10 * * * *' (every 10 min), '0 */3 * * *' "
                            "(every 3 hours), '0 9 * * 1-5' (weekdays at 9am). "
                            "Setting this switches the task to recurring (cron). "
                            "Do NOT use cron for intervals like every 17h — see "
                            "interval_seconds. Mutually exclusive with "
                            "interval_seconds + run_at."
                        ),
                    },
                    "interval_seconds": {
                        "type": "integer",
                        "minimum": 60,
                        "maximum": 31536000,
                        "description": (
                            "New real-time interval in seconds. Examples: 61200 "
                            "(every 17 hours), 19800 (every 5h30m), 259200 (every "
                            "3 days). Min 60s, max 31536000s. Setting this switches "
                            "the task to recurring (interval). Mutually exclusive "
                            "with schedule + run_at."
                        ),
                    },
                    "run_at": {
                        "type": "string",
                        "description": (
                            "New ISO datetime for a one-time task "
                            "(e.g. '2026-04-15T14:00:00'). Setting this "
                            "switches the task to one-time. Mutually exclusive "
                            "with schedule + interval_seconds."
                        ),
                    },
                    "timeout_seconds": {
                        "type": "integer",
                        "description": "New per-run timeout in seconds (optional)",
                    },
                    "notification_mode": {
                        "type": "string",
                        "enum": ["auto", "manual", "none"],
                        "description": (
                            "Change how the user is notified when this task completes. "
                            "See create_scheduled_task for the full auto/manual/none "
                            "decision matrix. Optional on edit — omit to leave unchanged."
                        ),
                    },
                    "notify_severity": {
                        "type": "string",
                        "enum": ["info", "success", "warning"],
                        "description": "Severity for the generic 'Task Complete' notification (only used in 'auto' mode)",
                    },
                    "model": _MODEL_ARG_SCHEMA,
                    "layer": _LAYER_ARG_SCHEMA,
                    "checks": _CHECKS_ARG_SCHEMA,
                    "timezone": _TIMEZONE_ARG_SCHEMA,
                },
                "required": ["task_id"],
            },
        ),
        Tool(
            name="get_task_history",
            description=(
                "Recent runs of this agent's tasks: run id, task, status, start, "
                "duration, the error when one failed, and `Left running: N` when "
                "a run ended with background commands or subagents still running "
                "(the run waited for them up to the task's timeout, then finished "
                "without their output; the session was kept for them). No prompt "
                "or output text: get_task_result shows the latest run's output, "
                "get_task the prompt. Runs are NOT stamped with the model they "
                "executed on — read it from list_tasks or get_task. With `agent`, "
                "reads another accessible agent's history instead."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "task_id": {
                        "type": "string",
                        "description": "Filter by specific task ID (optional)",
                    },
                    "limit": {
                        "type": "integer",
                        "description": "Max number of runs to return",
                        "default": 10,
                    },
                    "agent": _AGENT_ARG_SCHEMA,
                },
            },
        ),
        Tool(
            name="cancel_task_run",
            description=(
                "Cancel a running or queued task run. A run_task(wait=true) waiting on it "
                "returns at once with status cancelled."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "run_id": {"type": "string", "description": "Run ID to cancel"},
                },
                "required": ["run_id"],
            },
        ),
    ]


@server.call_tool()
async def call_tool(name: str, arguments: dict) -> list[TextContent]:
    try:
        if name == "create_scheduled_task":
            if "notification_mode" not in arguments:
                return [TextContent(
                    type="text",
                    text=(
                        "Error: notification_mode is required. "
                        "Pick 'auto' (system fires generic 'Task Complete'), "
                        "'manual' (task agent fires its own notification with results), "
                        "or 'none' (fully silent)."
                    ),
                )]
            has_schedule = bool(arguments.get("schedule"))
            has_interval = arguments.get("interval_seconds") is not None
            if has_schedule and has_interval:
                return [TextContent(
                    type="text",
                    text="Error: provide either `schedule` (cron) OR `interval_seconds` — not both.",
                )]
            if not has_schedule and not has_interval:
                return [TextContent(
                    type="text",
                    text=(
                        "Error: a recurring task needs `schedule` (cron, e.g. '0 9 * * 1-5') "
                        "or `interval_seconds` (e.g. 61200 for every 17 hours). "
                        "Use `interval_seconds` whenever the cadence doesn't divide 24 evenly."
                    ),
                )]
            body = {
                "name": arguments["name"],
                "agent": AGENT,
                "prompt": arguments["prompt"],
                "llm_mode": arguments.get("llm_mode", "cli"),
                "timeout_seconds": arguments.get("timeout_seconds", 600),
                "scope": arguments.get("scope") or SCOPE_DEFAULT,
                "notification_mode": arguments["notification_mode"],
                "notify_severity": arguments.get("notify_severity", "info"),
            }
            for k in ("model", "layer"):
                if arguments.get(k):
                    body[k] = arguments[k]
            if arguments.get("checks"):
                body["checks"] = list(arguments["checks"])
            if arguments.get("timezone"):
                body["user_tz"] = arguments["timezone"]
            if has_schedule:
                body["schedule"] = arguments["schedule"]
                timing_line = f"Schedule: {arguments['schedule']}"
            else:
                body["interval_seconds"] = arguments["interval_seconds"]
                timing_line = f"Interval: every {arguments['interval_seconds']}s"
            result = await _post("/v1/tasks/scheduled", body)
            return [TextContent(type="text", text="\n".join(x for x in [
                f"Created scheduled task: {result['task_id']}",
                timing_line,
                f"Timezone: {arguments['timezone']}" if arguments.get("timezone") else "",
                f"Scope: {arguments.get('scope') or SCOPE_DEFAULT}",
                f"Notification mode: {arguments['notification_mode']}",
                _lane_line(arguments),
                f"Name: {arguments['name']}",
            ] if x))]

        elif name == "create_one_time_task":
            if "notification_mode" not in arguments:
                return [TextContent(
                    type="text",
                    text=(
                        "Error: notification_mode is required. "
                        "Pick 'auto' (system fires generic 'Task Complete'), "
                        "'manual' (task agent fires its own notification with results), "
                        "or 'none' (fully silent)."
                    ),
                )]
            task_type = arguments.get("task_type", "one_time")
            if task_type == "one_time":
                if not arguments.get("run_at") and arguments.get("delay_seconds") is None:
                    return [TextContent(
                        type="text",
                        text="Error: one_time tasks require run_at or delay_seconds.",
                    )]
            elif task_type == "trigger":
                if arguments.get("run_at") or arguments.get("delay_seconds") is not None:
                    return [TextContent(
                        type="text",
                        text="Error: task_type='trigger' tasks cannot have run_at or delay_seconds; they fire only via webhook triggers.",
                    )]
            body = {
                "name": arguments["name"],
                "agent": AGENT,
                "prompt": arguments["prompt"],
                "run_at": arguments.get("run_at"),
                "delay_seconds": arguments.get("delay_seconds"),
                "llm_mode": arguments.get("llm_mode", "cli"),
                "timeout_seconds": arguments.get("timeout_seconds", 600),
                "scope": arguments.get("scope") or SCOPE_DEFAULT,
                "notification_mode": arguments["notification_mode"],
                "notify_severity": arguments.get("notify_severity", "info"),
                "task_type": task_type,
            }
            for k in ("model", "layer"):
                if arguments.get(k):
                    body[k] = arguments[k]
            if arguments.get("checks"):
                body["checks"] = list(arguments["checks"])
            if arguments.get("timezone"):
                body["user_tz"] = arguments["timezone"]
            result = await _post("/v1/tasks/one-time", body)
            if task_type == "trigger":
                timing = "on trigger fire"
            else:
                timing = (
                    f"at {arguments['run_at']}" if arguments.get("run_at")
                    else f"in {arguments['delay_seconds']}s"
                )
            return [TextContent(type="text", text="\n".join(x for x in [
                f"Created {task_type} task: {result['task_id']}",
                f"Runs: {timing}",
                f"Timezone: {arguments['timezone']}" if arguments.get("timezone") else "",
                _lane_line(arguments),
                f"Name: {arguments['name']}",
            ] if x))]

        elif name == "schedule_continuation":
            prompt = arguments.get("prompt", "")
            if not prompt:
                return [TextContent(type="text", text="Error: prompt is required.")]
            one_shot = [k for k in ("at", "in_seconds") if arguments.get(k) is not None]
            recurring = [k for k in ("repeat_cron", "repeat_interval_seconds")
                         if arguments.get(k) is not None]
            if len(one_shot) + len(recurring) != 1:
                return [TextContent(
                    type="text",
                    text=(
                        "Error: provide exactly ONE timing field — `at` or `in_seconds` "
                        "for a one-shot continuation, `repeat_cron` or "
                        "`repeat_interval_seconds` for a recurring one."
                    ),
                )]
            body = {
                "prompt": prompt,
                "name": arguments.get("name") or "",
                "at": arguments.get("at"),
                "in_seconds": arguments.get("in_seconds"),
                "repeat_cron": arguments.get("repeat_cron"),
                "repeat_interval_seconds": arguments.get("repeat_interval_seconds"),
                "max_runs": arguments.get("max_runs"),
                "until": arguments.get("until"),
            }
            result = await _post("/v1/continuations", body)
            lines = [
                f"Continuation scheduled: {result['task_id']}",
                f"Fires: {result.get('fires', '—')}",
            ]
            if result.get("max_runs"):
                lines.append(f"Bounded: max {result['max_runs']} run(s)"
                             + (f", until {result['until']}" if result.get("until") else ""))
            lines.append("Cancel any time with delete_task; it auto-cancels if this chat is deleted.")
            return [TextContent(type="text", text="\n".join(lines))]

        elif name == "run_task":
            task_id = arguments["task_id"]
            wait = arguments.get("wait", False)
            timeout_seconds = arguments.get("timeout_seconds", 600)

            r = await _post(f"/v1/tasks/{task_id}/run", {})
            run_id = r["run_id"]

            if not wait:
                return [TextContent(
                    type="text",
                    text=f"Task triggered: {task_id}\nRun ID: {run_id}\nRunning in background.",
                )]

            # Wait for completion via SSE stream. ``final_status`` is the
            # run's own word from the ``done`` frame (the proxy's run
            # vocabulary, read as a string over the API) — or this tool's
            # own WAIT_TIMED_OUT when the wait gave up listening first.
            url = f"{PROXY_URL}/v1/tasks/runs/{run_id}/stream"
            output_parts: list[str] = []

            async def _listen() -> str:
                async with httpx.AsyncClient() as client:
                    async with client.stream(
                        "GET", url,
                        headers=_headers(),
                        # A default is REQUIRED (httpx >= 0.28 raises ValueError
                        # for partial kwargs without one) — the old
                        # Timeout(connect=..., read=...) form crashed wait mode
                        # before the stream ever opened.
                        timeout=httpx.Timeout(
                            float(timeout_seconds + 30), connect=5.0,
                        ),
                    ) as resp:
                        async for line in resp.aiter_lines():
                            if not line.startswith("data:"):
                                continue
                            try:
                                ev = json.loads(line[5:].strip())
                            except json.JSONDecodeError:
                                continue
                            if ev.get("type") == "status":
                                if ev.get("status") == "pending":
                                    output_parts.append(
                                        "[queued — waiting for a free task "
                                        "slot]\n"
                                    )
                            elif ev.get("type") == "text":
                                output_parts.append(ev.get("text", ""))
                            elif ev.get("type") == "done":
                                return ev.get("status", "completed")
                return "unknown"

            try:
                # The wall-clock bound: httpx's read timeout is per read, and
                # the stream's keep-alive every 30 s resets it for the whole run.
                final_status = await asyncio.wait_for(_listen(), timeout_seconds)
            except (asyncio.TimeoutError, httpx.ReadTimeout):
                final_status = WAIT_TIMED_OUT
            except httpx.HTTPError as e:
                return [TextContent(
                    type="text",
                    text=(
                        f"Task triggered (run: {run_id}) but the wait stream "
                        f"failed: {e}. The run continues in the background — "
                        f"check get_task_result('{task_id}') later."
                    ),
                )]

            if final_status == WAIT_TIMED_OUT:
                return [TextContent(
                    type="text",
                    text=(
                        f"Task {task_id} (run: {run_id}) is still running after {timeout_seconds}s. "
                        f"It continues in the background. Check get_task_result(task_id) later "
                        f"for the result."
                    ),
                )]

            output = "".join(output_parts) or "(no output)"
            return [TextContent(
                type="text",
                text=(
                    f"Task run ended: {task_id} (run: {run_id})\n"
                    f"Status: {final_status}\n\n"
                    f"Output:\n{output}"
                ),
            )]

        elif name == "get_task_result":
            task_id = arguments["task_id"]
            params: dict = {"task_id": task_id, "limit": 1, **_agent_param(arguments)}
            result = await _get("/v1/tasks/runs", params=params)
            runs = result.get("runs", [])
            if not runs:
                return [TextContent(type="text", text=f"No runs found for task {task_id}.")]
            r = runs[0]
            duration = f"{r['duration_ms']}ms" if r.get("duration_ms") else "—"
            lines = [
                f"Task: {task_id}",
                f"Run: {r['id']}",
                f"Status: {r['status']}",
                f"Started: {r.get('started_at', '—')}",
                f"Completed: {r.get('completed_at', '—')}",
                f"Duration: {duration}",
            ]
            if r.get("error_message"):
                lines.append(f"Error: {r['error_message']}")
            if r.get("output_text"):
                lines.append(f"\nOutput:\n{r['output_text']}")
            return [TextContent(type="text", text="\n".join(lines))]

        elif name == "list_tasks":
            result = await _get("/v1/tasks", params=_agent_param(arguments))
            tasks = result.get("tasks", [])
            if not tasks:
                return [TextContent(type="text", text="No tasks found.")]
            lines = [f"Tasks ({len(tasks)} total):"]
            for t in tasks:
                next_run = t.get("next_run_time") or "—"
                status = "active" if t.get("enabled") else "paused"
                task_type = t.get("task_type", "task")
                lines.append(
                    f"  [{task_type}] {t['id']} — {t['name']} "
                    f"({t['agent']}, {_fires_info(t)}, {status}, next: {next_run}"
                    f"{_runs_on(t)})"
                )
            return [TextContent(type="text", text="\n".join(lines))]

        elif name == "get_task":
            task_id = arguments["task_id"]
            t = await _get(f"/v1/tasks/{task_id}")
            status = "active" if t.get("enabled") else "paused"
            # The zone a cron, an interval anchor or a naive run_at is read
            # in: the row's own, else the platform's (which the row keeps
            # following). Meaningless for a trigger-type task.
            tz_note = ""
            if t.get("task_type") != "trigger":
                if t.get("user_tz"):
                    tz_note = f" (timezone {t['user_tz']})"
                elif t.get("effective_tz"):
                    tz_note = f" (platform timezone {t['effective_tz']})"
            lines = [
                f"Task: {t['id']} — {t['name']}",
                f"Type: {t.get('task_type', 'task')}   Agent: {t.get('agent')}   "
                f"Scope: {t.get('scope')}   Created by: {t.get('created_by') or '—'} "
                f"at {t.get('created_at') or '—'}",
                f"Fires: {_fires_info(t)}{tz_note}",
                f"Status: {status}   Next run: {t.get('next_run_time') or '—'}   "
                f"Runs so far: {t.get('run_count', 0)}"
                + (f" of {t['max_runs']}" if t.get("max_runs") else "")
                + (f"   Until: {t['until_at']}" if t.get("until_at") else ""),
                f"Timeout: {t.get('timeout_seconds', '—')}s   "
                f"Notification: {t.get('notification_mode', '—')}"
                + (f" ({t['notify_severity']})" if t.get("notify_severity") else "")
                + (f"   Target chat: {t['target_chat_id']}" if t.get("target_chat_id") else ""),
            ]
            runs_on = _runs_on(t)
            if runs_on:
                lines.append("Runs on: " + runs_on[len(", runs on: "):]
                             if runs_on.startswith(", runs on: ") else "Runs" + runs_on[len(", runs"):])
            elif t.get("task_type") == "app":
                lines.append("Runs on: no model (an app handler, no LLM turn)")
            for w in t.get("pin_warnings") or []:
                lines.append(f"Warning: {w}")
            if t.get("task_type") == "trigger":
                trig = t.get("triggers") or []
                if trig:
                    lines.append("Triggers pointing at it:")
                    for r in trig:
                        lines.append(
                            f"  • {r.get('name')} [{r.get('scope')}] "
                            f"[{'active' if r.get('enabled') else 'paused'}] "
                            f"fires={r.get('fired_count', 0)} last={r.get('last_fired_at') or 'never'} "
                            f"{'vendor subscription' if r.get('subscription_id') else r.get('webhook_path') or ''} "
                            f"id={r.get('id')}"
                        )
                else:
                    lines.append("Triggers pointing at it: none yet — wire one with "
                                 f"create_trigger(task_id='{t['id']}')")
            if t.get("on_complete_agent"):
                lines.append(f"On complete: {t['on_complete_agent']} ← {t.get('on_complete_prompt') or ''}")
            lines.append("")
            lines.append("Prompt:")
            lines.append("```")
            lines.append(t.get("prompt") or "(empty)")
            lines.append("```")
            return [TextContent(type="text", text="\n".join(lines))]

        elif name == "delete_task":
            task_id = arguments["task_id"]
            await _delete(f"/v1/tasks/{task_id}")
            return [TextContent(type="text", text=f"Deleted task: {task_id}")]

        elif name == "pause_task":
            task_id = arguments["task_id"]
            await _post(f"/v1/tasks/{task_id}/pause", {})
            return [TextContent(
                type="text",
                text=f"Paused task: {task_id}. It will not fire until resumed.",
            )]

        elif name == "resume_task":
            task_id = arguments["task_id"]
            await _post(f"/v1/tasks/{task_id}/resume", {})
            return [TextContent(
                type="text",
                text=(
                    f"Resumed task: {task_id}. "
                    "If this is a one-time task whose scheduled time has passed, "
                    "it will not fire automatically — the user can run it manually."
                ),
            )]

        elif name == "edit_task":
            task_id = arguments["task_id"]
            # Build the edit body from any provided field besides task_id.
            # ``model``/``layer`` are included even when empty — "" is the
            # explicit "drop the pin, go back to the agent's default" signal.
            edit_keys = (
                "name", "prompt", "schedule", "run_at", "interval_seconds",
                "timeout_seconds", "notification_mode", "notify_severity",
                "model", "layer", "checks",
            )
            body = {k: arguments[k] for k in edit_keys if k in arguments}
            # The zone travels as user_tz on the wire; a zone edit alone is
            # a valid edit (the proxy re-registers on it).
            if arguments.get("timezone"):
                body["user_tz"] = arguments["timezone"]
            if not body:
                return [TextContent(
                    type="text",
                    text="Error: provide at least one field to edit besides task_id.",
                )]
            timing_set = [k for k in ("schedule", "run_at", "interval_seconds") if body.get(k)]
            if len(timing_set) > 1:
                return [TextContent(
                    type="text",
                    text=(
                        f"Error: schedule, run_at, and interval_seconds are mutually exclusive — "
                        f"set only one. Got: {', '.join(timing_set)}."
                    ),
                )]
            await _post(f"/v1/tasks/{task_id}/edit", body)
            changed = ", ".join("timezone" if k == "user_tz" else k for k in body)
            lane = _lane_line(body, cleared=True)
            return [TextContent(
                type="text",
                text="\n".join(x for x in [
                    f"Updated task {task_id} ({changed}).",
                    lane,
                ] if x),
            )]

        elif name == "get_task_history":
            # Delegate runs stay visible here (the dashboard's Tasks page
            # excludes them — they live in the chat history there).
            params: dict = {"limit": arguments.get("limit", 10),
                            "include_delegates": "true",
                            **_agent_param(arguments)}
            if arguments.get("task_id"):
                params["task_id"] = arguments["task_id"]
            result = await _get("/v1/tasks/runs", params=params)
            runs = result.get("runs", [])
            if not runs:
                return [TextContent(type="text", text="No runs found.")]
            cross_agent = "agent" in arguments  # label rows on cross-agent reads
            lines = [f"Recent runs ({len(runs)} of {result.get('total', '?')} total):"]
            for r in runs:
                duration = f"{r['duration_ms']}ms" if r.get("duration_ms") else "—"
                agent_tag = f" ({r.get('agent', '?')})" if cross_agent else ""
                lines.append(
                    f"  {r['id']} — {r['task_id']}{agent_tag} [{r['status']}] "
                    f"started={r.get('started_at', '—')} duration={duration}"
                )
                if r.get("error_message"):
                    lines.append(f"    Error: {r['error_message']}")
                if r.get("background_pending"):
                    lines.append(
                        f"    Left running: {r['background_pending']} background "
                        f"command(s)/subagent(s) past the task's timeout; the session "
                        f"was kept for them, their output is not in this run"
                    )
            return [TextContent(type="text", text="\n".join(lines))]

        elif name == "cancel_task_run":
            run_id = arguments["run_id"]
            result = await _post(f"/v1/tasks/runs/{run_id}/cancel", {})
            return [TextContent(
                type="text",
                text=f"Cancel result for {run_id}: {result.get('status', 'unknown')}",
            )]

        else:
            return [TextContent(type="text", text=f"Unknown tool: {name}")]

    except httpx.HTTPStatusError as e:
        # The proxy's refusals carry their sentence under "detail": show the
        # sentence itself, not the JSON around it; anything else verbatim.
        error_body = e.response.text
        try:
            detail = json.loads(error_body).get("detail")
        except (ValueError, AttributeError):
            detail = None
        return [TextContent(
            type="text",
            text=f"API error {e.response.status_code}: {detail if isinstance(detail, str) else error_body}",
        )]
    except Exception as e:
        return [TextContent(type="text", text=f"Error: {e}")]


async def main():
    async with stdio_server() as (read_stream, write_stream):
        await server.run(
            read_stream,
            write_stream,
            server.create_initialization_options(),
        )


if __name__ == "__main__":
    asyncio.run(main())
