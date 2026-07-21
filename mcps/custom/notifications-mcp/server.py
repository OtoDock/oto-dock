"""Notifications MCP Server.

Stdio MCP server providing notification tools for Claude Code agents.
Communicates with the proxy REST API to create/list/cancel notifications.

Env vars (set in per-agent mcp-config.json):
  NOTIF_MCP_AGENT       - agent name (for X-Agent-Name header)
  PROXY_URL   - proxy base URL (default: http://localhost:8400)
  NOTIF_MCP_API_KEY     - auth key (falls back to PROXY_API_KEY from process env)
"""

import asyncio
import os

import httpx
from mcp.server import Server
from mcp.types import TextContent, Tool

AGENT = os.environ.get("NOTIF_MCP_AGENT", "system-admin")
PROXY_URL = os.environ.get("PROXY_URL", "http://localhost:8400").rstrip("/")
API_KEY = os.environ.get("NOTIF_MCP_API_KEY") or os.environ.get("PROXY_API_KEY", "")

# Per-agent default scope. Drives both the tool-schema default the LLM
# sees AND the server-side fallback in ``_handle_create``. Falls back through
# PROXY_TASK_SCOPE (the session's task scope) and OTO_SCOPE before landing on
# the safe "user" default.
_DEFAULT_SCOPE = (
    os.environ.get("OTO_DEFAULT_SCOPE")
    or os.environ.get("PROXY_TASK_SCOPE")
    or os.environ.get("OTO_SCOPE")
    or "user"
)

# visibility-modes: the agent's mode scopes (Personal-only → ["user"], Shared-only
# → ["agent"], collaborative → both). Filters the scope arg enum so the LLM can't
# pick a scope this agent lacks. Unset → both. (The `target_scope`-style "global"
# broadcast is a separate concern and not filtered here.)
AVAILABLE_SCOPES = [
    s for s in (os.environ.get("OTO_AVAILABLE_SCOPES", "") or "").split(":")
    if s in ("user", "agent")
] or ["user", "agent"]
SCOPE_DEFAULT = _DEFAULT_SCOPE if _DEFAULT_SCOPE in AVAILABLE_SCOPES else AVAILABLE_SCOPES[0]

server = Server("notifications-mcp")

_client = httpx.AsyncClient(timeout=30)


def _headers() -> dict:
    return {
        "Authorization": f"Bearer {API_KEY}",
        "X-Agent-Name": AGENT,
        "Content-Type": "application/json",
    }


async def _post(path: str, body: dict) -> dict:
    resp = await _client.post(f"{PROXY_URL}{path}", json=body, headers=_headers())
    resp.raise_for_status()
    return resp.json()


async def _get(path: str, params: dict | None = None) -> dict:
    resp = await _client.get(f"{PROXY_URL}{path}", params=params, headers=_headers())
    resp.raise_for_status()
    return resp.json()


async def _delete(path: str) -> dict:
    resp = await _client.delete(f"{PROXY_URL}{path}", headers=_headers())
    resp.raise_for_status()
    return resp.json()


async def _get_current_session() -> dict:
    """Get the current session context (username, chat_id, etc.)."""
    try:
        return await _get("/v1/session/current")
    except Exception:
        return {}


@server.list_tools()
async def list_tools() -> list[Tool]:
    return [
        Tool(
            name="create_notification",
            description=(
                "Create a notification for a user. Notifications are delivered as push "
                "notifications (phone/browser) and appear in the notification inbox. "
                "Use severity carefully: 'danger' triggers an alarm that loops until dismissed."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "title": {
                        "type": "string",
                        "description": "Short notification title (shown in push + inbox)",
                    },
                    "body": {
                        "type": "string",
                        "description": "Notification body text with details",
                    },
                    "severity": {
                        "type": "string",
                        "enum": ["info", "success", "warning", "danger"],
                        "default": "info",
                        "description": (
                            "info=chime, success=chime, warning=sound, "
                            "danger=alarm loop+TTS until dismissed"
                        ),
                    },
                    "type": {
                        "type": "string",
                        "enum": ["one_time", "recurring"],
                        "default": "one_time",
                        "description": "one_time (fires once) or recurring (fires on schedule)",
                    },
                    "run_at": {
                        "type": "string",
                        "description": (
                            "ISO datetime for one_time scheduled notifications "
                            "in the user's local timezone — the one shown in the "
                            "[Current time: ...] line of the user message. "
                            "Example: '2026-03-22T09:00:00'. Prefer naive "
                            "(no offset) when matching the user's wall-clock "
                            "intent — the proxy interprets it in the user's "
                            "local timezone automatically. Explicit offsets "
                            "like '+03:00' or 'Z' are respected exactly. "
                            "Omit for immediate delivery."
                        ),
                    },
                    "schedule": {
                        "type": "string",
                        "description": (
                            "Standard 5-field POSIX cron for recurring notifications. "
                            "Use for WALL-CLOCK schedules. Examples: "
                            "'0 9 * * *' (daily at 9am), '*/10 * * * *' (every 10 minutes), "
                            "'0 */3 * * *' (every 3 hours — works because 3 divides 24), "
                            "'*/15 9-17 * * 1-5' (every 15 minutes during business hours, weekdays). "
                            "DO NOT use cron for intervals like every 17h or every 5h that don't "
                            "divide 24 evenly — use interval_seconds instead. "
                            "Mutually exclusive with interval_seconds and run_at."
                        ),
                    },
                    "interval_seconds": {
                        "type": "integer",
                        "minimum": 60,
                        "maximum": 31536000,
                        "description": (
                            "Recurring real-time interval in seconds, anchored at creation. "
                            "Examples: 3600 (every hour), 61200 (every 17 hours), 19800 "
                            "(every 5h30m), 259200 (every 3 days). Min 60s, max 31536000s. "
                            "Use when the cadence does NOT divide 24 cleanly. "
                            "Mutually exclusive with schedule and run_at. First fire is one "
                            "interval after creation, never on creation itself."
                        ),
                    },
                    "scope": {
                        "type": "string",
                        "enum": AVAILABLE_SCOPES,
                        "default": SCOPE_DEFAULT,
                        "description": (
                            f"Default for this agent: `{_DEFAULT_SCOPE}`. "
                            "'user' = current user only; "
                            "'agent' = all users of this agent (requires manager/admin). "
                            "Omit to use the agent's default; override only when the user explicitly "
                            "wants the other scope."
                        ),
                    },
                    "target": {
                        "type": "string",
                        "description": (
                            "For scope=agent: the agent name (defaults to this "
                            "agent). Ignored for scope=user — the notification "
                            "always goes to the current user (identity is taken "
                            "from the session server-side)."
                        ),
                    },
                },
                "required": ["title", "body"],
            },
        ),
        Tool(
            name="list_notifications",
            description=(
                "List scheduled notifications (both active and paused). "
                "Each entry shows status (active or paused), severity, "
                "schedule/run_at, and id. Use this to find a notification "
                "before calling pause_notification, resume_notification, "
                "or cancel_notification."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "scope": {
                        "type": "string",
                        "enum": AVAILABLE_SCOPES,
                        "description": "Filter by scope (optional)",
                    },
                },
            },
        ),
        Tool(
            name="cancel_notification",
            description=(
                "Permanently delete a notification. This cannot be undone. "
                "To temporarily stop firing without losing the notification, "
                "use pause_notification instead."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "id": {
                        "type": "string",
                        "description": "The notification ID to delete",
                    },
                },
                "required": ["id"],
            },
        ),
        Tool(
            name="pause_notification",
            description=(
                "Pause a scheduled notification without deleting it. "
                "The notification stays in the system but won't fire on its "
                "schedule until resumed via resume_notification."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "id": {
                        "type": "string",
                        "description": "The notification ID to pause",
                    },
                },
                "required": ["id"],
            },
        ),
        Tool(
            name="resume_notification",
            description=(
                "Resume a paused notification so it fires on its schedule again. "
                "For one-time notifications whose scheduled time has already "
                "passed, the notification will not fire automatically — the user "
                "can fire it manually from the dashboard if they want."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "id": {
                        "type": "string",
                        "description": "The notification ID to resume",
                    },
                },
                "required": ["id"],
            },
        ),
        Tool(
            name="edit_notification",
            description=(
                "Edit a notification in place — change its schedule, run time, "
                "title, body, or severity without deleting and recreating it. "
                "At least one editable field besides id must be provided. "
                "schedule, interval_seconds, and run_at are mutually exclusive: "
                "setting one switches the notification's mode and clears the others."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "id": {
                        "type": "string",
                        "description": "The notification ID to edit",
                    },
                    "title": {"type": "string", "description": "New title (optional)"},
                    "body": {"type": "string", "description": "New body text (optional)"},
                    "severity": {
                        "type": "string",
                        "enum": ["info", "success", "warning", "danger"],
                        "description": "New severity (optional)",
                    },
                    "schedule": {
                        "type": "string",
                        "description": (
                            "New cron expression for a recurring notification. "
                            "Use for WALL-CLOCK schedules. Examples: "
                            "'*/10 * * * *' (every 10 min), '0 */3 * * *' "
                            "(every 3 hours — works because 3 divides 24), "
                            "'0 9 * * *' (daily at 9am). Setting this switches to "
                            "recurring (cron). Mutually exclusive with interval_seconds + run_at."
                        ),
                    },
                    "interval_seconds": {
                        "type": "integer",
                        "minimum": 60,
                        "maximum": 31536000,
                        "description": (
                            "New real-time interval in seconds. Examples: 61200 "
                            "(every 17h), 19800 (every 5h30m). Setting this switches "
                            "to recurring (interval). Mutually exclusive with schedule + run_at."
                        ),
                    },
                    "run_at": {
                        "type": "string",
                        "description": (
                            "New ISO datetime for a one-time notification. "
                            "Setting this switches the notification to one_time. "
                            "Mutually exclusive with schedule + interval_seconds."
                        ),
                    },
                },
                "required": ["id"],
            },
        ),
    ]


@server.call_tool()
async def call_tool(name: str, arguments: dict) -> list[TextContent]:
    if name == "create_notification":
        return await _handle_create(arguments)
    elif name == "list_notifications":
        return await _handle_list(arguments)
    elif name == "cancel_notification":
        return await _handle_cancel(arguments)
    elif name == "pause_notification":
        return await _handle_pause(arguments)
    elif name == "resume_notification":
        return await _handle_resume(arguments)
    elif name == "edit_notification":
        return await _handle_edit(arguments)
    else:
        return [TextContent(type="text", text=f"Unknown tool: {name}")]


async def _handle_create(args: dict) -> list[TextContent]:
    title = args["title"]
    body = args["body"]
    severity = args.get("severity", "info")
    ntype = args.get("type", "one_time")
    # The per-agent default scope drives the fallback when the agent omits
    # the scope arg. The API role-gate still enforces what each role can
    # actually write — default is convenience; the server gate is law.
    scope = args.get("scope") or _DEFAULT_SCOPE
    target = args.get("target")
    run_at = args.get("run_at")
    schedule = args.get("schedule")
    interval_seconds = args.get("interval_seconds")

    # Validate mutual exclusivity locally so we surface a clear error before
    # the round-trip to the API.
    non_null_timing = sum(
        1 for v in (run_at, schedule, interval_seconds) if v
    )
    if non_null_timing > 1:
        return [TextContent(
            type="text",
            text=(
                "Error: run_at, schedule, and interval_seconds are mutually exclusive — "
                "set only one (or none for immediate fire)."
            ),
        )]
    if ntype == "recurring" and not schedule and interval_seconds is None:
        return [TextContent(
            type="text",
            text=(
                "Error: recurring notifications need either `schedule` (cron, e.g. "
                "'0 9 * * *') or `interval_seconds` (e.g. 61200 for every 17 hours)."
            ),
        )]

    # Resolve session context for deep linking (chat_id only). Identity is
    # token-authoritative server-side: for a user-scoped notification the proxy
    # fills `target` from the caller's session token, so this MCP must not
    # guess or assert it (a no-user/phone session is rejected by the proxy).
    session = await _get_current_session()

    # For agent-scope without explicit target, use this agent
    if scope == "agent" and not target:
        target = AGENT

    payload = {
        "title": title,
        "body": body,
        "severity": severity,
        "scope": scope,
        "target": target,
        "notification_type": ntype,
        "source": "mcp",
    }
    if run_at:
        payload["run_at"] = run_at
    if schedule:
        payload["schedule"] = schedule
    if interval_seconds is not None:
        payload["interval_seconds"] = interval_seconds

    # Deep link context — links the notification back to the originating chat.
    # For scheduled/recurring, this links to the chat where it was set up.
    # Task sessions: chat_id from env var injected by scheduler
    deep_chat_id = os.environ.get("NOTIF_MCP_CHAT_ID")
    if not deep_chat_id:
        # Interactive sessions: chat_id from /v1/session/current
        deep_chat_id = session.get("chat_id")
    if deep_chat_id:
        payload["agent_slug"] = AGENT
        payload["chat_id"] = deep_chat_id

    try:
        result = await _post("/v1/notifications", payload)
        notif = result.get("notification", {})
        nid = notif.get("id", "unknown")

        if run_at or schedule:
            when = f"scheduled for {run_at}" if run_at else f"recurring ({schedule})"
            return [TextContent(
                type="text",
                text=f"Notification created (ID: {nid}). {when}. "
                     f"Severity: {severity}, scope: {scope}.",
            )]
        else:
            return [TextContent(
                type="text",
                text=f"Notification sent immediately (ID: {nid}). "
                     f"Severity: {severity}, scope: {scope}.",
            )]
    except httpx.HTTPStatusError as e:
        detail = e.response.text if e.response else str(e)
        return [TextContent(type="text", text=f"Failed to create notification: {detail}")]
    except Exception as e:
        return [TextContent(type="text", text=f"Error: {e}")]


async def _handle_list(args: dict) -> list[TextContent]:
    params = {}
    if "scope" in args:
        params["scope"] = args["scope"]
    try:
        result = await _get("/v1/notifications", params)

        # API returns 'notifications' for API key or 'deliveries' for user
        items = result.get("notifications", result.get("deliveries", []))
        if not items:
            return [TextContent(type="text", text="No notifications found.")]

        lines = []
        for n in items:
            nid = n.get("id", "?")
            title = n.get("title", "Untitled")
            severity = n.get("severity", "info")
            ntype = n.get("notification_type", n.get("source", ""))
            schedule = n.get("schedule", "")
            run_at = n.get("run_at", "")
            enabled = n.get("enabled", 1)
            status = "active" if enabled else "paused"
            timing = f" [{schedule}]" if schedule else (f" [run_at={run_at}]" if run_at else "")
            lines.append(f"- [{severity}] {title}{timing} (id={nid}, type={ntype}, {status})")

        return [TextContent(type="text", text="\n".join(lines))]
    except Exception as e:
        return [TextContent(type="text", text=f"Error listing notifications: {e}")]


async def _handle_cancel(args: dict) -> list[TextContent]:
    nid = args["id"]
    try:
        await _delete(f"/v1/notifications/{nid}")
        return [TextContent(type="text", text=f"Notification {nid} deleted.")]
    except httpx.HTTPStatusError as e:
        if e.response.status_code == 404:
            return [TextContent(type="text", text=f"Notification {nid} not found.")]
        return [TextContent(type="text", text=f"Failed to delete: {e.response.text}")]
    except Exception as e:
        return [TextContent(type="text", text=f"Error: {e}")]


async def _handle_pause(args: dict) -> list[TextContent]:
    nid = args["id"]
    try:
        resp = await _client.post(
            f"{PROXY_URL}/v1/notifications/{nid}/pause",
            json={},
            headers=_headers(),
        )
        resp.raise_for_status()
        return [TextContent(
            type="text",
            text=f"Notification {nid} paused. It will not fire until resumed.",
        )]
    except httpx.HTTPStatusError as e:
        if e.response.status_code == 404:
            return [TextContent(type="text", text=f"Notification {nid} not found.")]
        return [TextContent(type="text", text=f"Failed to pause: {e.response.text}")]
    except Exception as e:
        return [TextContent(type="text", text=f"Error: {e}")]


async def _handle_resume(args: dict) -> list[TextContent]:
    nid = args["id"]
    try:
        resp = await _client.post(
            f"{PROXY_URL}/v1/notifications/{nid}/resume",
            json={},
            headers=_headers(),
        )
        resp.raise_for_status()
        return [TextContent(
            type="text",
            text=(
                f"Notification {nid} resumed. "
                "If this is a one-time notification whose scheduled time has passed, "
                "it will not fire automatically — the user can fire it manually."
            ),
        )]
    except httpx.HTTPStatusError as e:
        if e.response.status_code == 404:
            return [TextContent(type="text", text=f"Notification {nid} not found.")]
        return [TextContent(type="text", text=f"Failed to resume: {e.response.text}")]
    except Exception as e:
        return [TextContent(type="text", text=f"Error: {e}")]


async def _handle_edit(args: dict) -> list[TextContent]:
    nid = args["id"]
    edit_keys = ("title", "body", "severity", "schedule", "run_at", "interval_seconds")
    body = {k: args[k] for k in edit_keys if k in args}
    if not body:
        return [TextContent(
            type="text",
            text="Error: provide at least one field to edit besides id.",
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
    try:
        resp = await _client.post(
            f"{PROXY_URL}/v1/notifications/{nid}/edit",
            json=body,
            headers=_headers(),
        )
        resp.raise_for_status()
        changed = ", ".join(body.keys())
        return [TextContent(
            type="text",
            text=f"Updated notification {nid} ({changed}).",
        )]
    except httpx.HTTPStatusError as e:
        if e.response.status_code == 404:
            return [TextContent(type="text", text=f"Notification {nid} not found.")]
        return [TextContent(type="text", text=f"Failed to edit: {e.response.text}")]
    except Exception as e:
        return [TextContent(type="text", text=f"Error: {e}")]


async def main():
    from mcp.server.stdio import stdio_server
    async with stdio_server() as (read_stream, write_stream):
        await server.run(read_stream, write_stream, server.create_initialization_options())


if __name__ == "__main__":
    asyncio.run(main())
