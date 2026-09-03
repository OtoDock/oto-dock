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
        "agent's everyday default stays cheap, or the reverse. Must be a "
        "model enabled on the task's execution layer — read the valid ids "
        "off this agent's `layers:` line in the prompt. Omit to inherit the "
        "agent's default (recommended). On `edit_task`, pass \"\" to clear "
        "an existing pin."
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
                "Create a recurring task — fire-and-forget only. "
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
                "Set wait=true to block and get the output inline (good for short tasks). "
                "Set wait=false (default) to fire-and-forget."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "task_id": {"type": "string", "description": "ID of the existing task to run"},
                    "wait": {
                        "type": "boolean",
                        "description": "If true, block until the task completes and return output. Default false.",
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
                "time, status (active or paused), and what its runs execute on — "
                "the effective model and execution layer, marked [pinned] when the "
                "task carries its own override or [agent default] when it follows "
                "the agent's current setting. This is the authoritative read for "
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
                },
                "required": ["task_id"],
            },
        ),
        Tool(
            name="get_task_history",
            description=(
                "Get recent run history for this agent's tasks. With `agent`, "
                "reads another accessible agent's history instead — runs carry "
                "their prompt + output text, exactly what your user would see "
                "on that agent's Task History page. Runs are NOT stamped with "
                "the model they executed on — a task's current model/layer "
                "(pinned or agent default) is read via list_tasks."
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
            description="Cancel a currently running task execution.",
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

            # Wait for completion via SSE stream
            url = f"{PROXY_URL}/v1/tasks/runs/{run_id}/stream"
            output_parts: list[str] = []
            final_status = "unknown"
            try:
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
                                final_status = ev.get("status", "completed")
                                break
            except httpx.ReadTimeout:
                final_status = "timeout"
            except httpx.HTTPError as e:
                return [TextContent(
                    type="text",
                    text=(
                        f"Task triggered (run: {run_id}) but the wait stream "
                        f"failed: {e}. The run continues in the background — "
                        f"check get_task_result('{task_id}') later."
                    ),
                )]

            if final_status == "timeout":
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
                    f"Task run completed: {task_id} (run: {run_id})\n"
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
                schedule_info = (
                    t.get("schedule")
                    or t.get("run_at")
                    or (f"every {t['interval_seconds']}s" if t.get("interval_seconds") else None)
                    or (f"delay {t['delay_seconds']}s" if t.get("delay_seconds") is not None else None)
                    or "one-time"
                )
                next_run = t.get("next_run_time") or "—"
                status = "active" if t.get("enabled") else "paused"
                task_type = t.get("task_type", "task")
                # What the task's runs execute on. The backend resolves
                # effective_model/effective_execution_path (task pin, else the
                # agent's CURRENT default); the raw override_* fields tell pin
                # from default. An unresolved default comes back "" — render
                # nothing rather than a wrong claim (matches the dashboard).
                eff_model = t.get("effective_model") or ""
                model_tag = "pinned" if t.get("override_model") else "agent default"
                layer_bit = (
                    f" via {t['effective_execution_path']} [pinned layer]"
                    if t.get("override_execution_path")
                       and t.get("effective_execution_path") else ""
                )
                runs_on = (
                    f", runs on: {eff_model} [{model_tag}]{layer_bit}"
                    if eff_model else (layer_bit and f", runs{layer_bit}")
                )
                lines.append(
                    f"  [{task_type}] {t['id']} — {t['name']} "
                    f"({t['agent']}, {schedule_info}, {status}, next: {next_run}"
                    f"{runs_on})"
                )
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
                "model", "layer",
            )
            body = {k: arguments[k] for k in edit_keys if k in arguments}
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
            changed = ", ".join(body.keys())
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
        error_body = e.response.text
        return [TextContent(
            type="text",
            text=f"API error {e.response.status_code}: {error_body}",
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
