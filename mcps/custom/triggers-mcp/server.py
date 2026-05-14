"""Triggers MCP Server.

Stdio MCP server providing webhook-trigger management tools to Claude Code
agents. Communicates with the proxy REST API to CRUD trigger definitions
and to fire-test triggers.

Triggers are event-driven webhooks fired by external systems. They
optionally fire a task (referenced by id, must be ``task_type='trigger'``)
and/or a notification (inline title/body with ``{{placeholder}}``
substitution from the webhook payload).

Two scopes:

  user   — personal automations (GitHub PR alerts, Stripe payment notif).
           Created by any user with agent access; only the creator (and
           admin) can mutate. Authenticated via the user's user_api_keys.
  agent  — manager-managed business events (server alerts, deploy hooks).
           Authenticated via agent_api_keys.

Env vars (set in per-agent mcp-config.json by manifest agent_env):
  TRIG_MCP_AGENT         - agent name (X-Agent-Name header)
  PROXY_URL     - proxy base URL (default: http://localhost:8400)
  PROXY_API_KEY          - per-session JWT for proxy auth. Identity is
                           token-authoritative server-side; this MCP no longer
                           asserts a user via X-On-Behalf-Of.
"""

import asyncio
import json
import os

import httpx
from mcp.server import Server
from mcp.types import TextContent, Tool

AGENT = os.environ.get("TRIG_MCP_AGENT", "")
PROXY_URL = os.environ.get("PROXY_URL", "http://localhost:8400").rstrip("/")
API_KEY = os.environ.get("PROXY_API_KEY", "")

server = Server("triggers-mcp")
_client = httpx.AsyncClient(timeout=30)


def _headers() -> dict:
    # Identity is token-authoritative server-side: the proxy attributes the
    # acting user from this MCP's per-session JWT (PROXY_API_KEY). The MCP no
    # longer asserts identity via X-On-Behalf-Of.
    return {
        "Authorization": f"Bearer {API_KEY}",
        "X-Agent-Name": AGENT,
        "Content-Type": "application/json",
    }


async def _post(path: str, body: dict) -> dict:
    r = await _client.post(
        f"{PROXY_URL}{path}", json=body, headers=_headers(),
    )
    if r.status_code >= 400:
        try:
            detail = r.json().get("detail", r.text)
        except Exception:
            detail = r.text
        raise RuntimeError(f"HTTP {r.status_code}: {detail}")
    return r.json()


async def _patch(path: str, body: dict) -> dict:
    r = await _client.patch(
        f"{PROXY_URL}{path}", json=body, headers=_headers(),
    )
    if r.status_code >= 400:
        try:
            detail = r.json().get("detail", r.text)
        except Exception:
            detail = r.text
        raise RuntimeError(f"HTTP {r.status_code}: {detail}")
    return r.json()


async def _delete(path: str) -> dict:
    r = await _client.delete(f"{PROXY_URL}{path}", headers=_headers())
    if r.status_code >= 400:
        try:
            detail = r.json().get("detail", r.text)
        except Exception:
            detail = r.text
        raise RuntimeError(f"HTTP {r.status_code}: {detail}")
    return r.json()


async def _get(path: str, params: dict | None = None) -> dict:
    r = await _client.get(
        f"{PROXY_URL}{path}", params=params, headers=_headers(),
    )
    if r.status_code >= 400:
        try:
            detail = r.json().get("detail", r.text)
        except Exception:
            detail = r.text
        raise RuntimeError(f"HTTP {r.status_code}: {detail}")
    return r.json()


# =====================================================================
# Tool definitions
# =====================================================================


@server.list_tools()
async def list_tools() -> list[Tool]:
    return [
        Tool(
            name="create_trigger",
            description=(
                "Create a webhook trigger that external systems (GitHub, Stripe, "
                "Linear, IoT, Zapier, etc.) can call to fire a task and/or a "
                "notification. Returns the trigger record + the webhook URL the "
                "user should configure in the external system. The user MUST "
                "ALSO create an API key (via dashboard) that the external system "
                "passes as `Authorization: Bearer otok_…`. The master key is "
                "rejected on webhook fires for security.\n\n"
                "Use scope='user' (default) for personal automations like "
                "'notify me when my PR is merged'. Use scope='agent' (manager+ "
                "only) for business events that affect the whole team like "
                "server alerts. Cross-scope task linkage is forbidden — a "
                "user-scoped trigger can only run a user-scoped task and so on.\n\n"
                "If you want to run an agent task, first create it with "
                "task_type='trigger' (no schedule, no run_at), then pass its id "
                "as task_id here. The webhook payload is substituted into "
                "`{{placeholder}}` tokens in the task prompt and notify "
                "title/body."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "name": {
                        "type": "string",
                        "description": "Human-readable trigger name (used to derive slug if not supplied)",
                    },
                    "scope": {
                        "type": "string",
                        "enum": AVAILABLE_SCOPES,
                        "default": SCOPE_DEFAULT,
                        "description": (
                            f"Default for this agent: `{_default_scope()}`. "
                            "user = personal automation (creator-only mutate). "
                            "agent = manager-managed (requires manager/admin role). "
                            "Omit to use the agent's default; override only when the user "
                            "explicitly wants the other scope."
                        ),
                    },
                    "slug": {
                        "type": "string",
                        "description": (
                            "URL-safe slug (1-64 chars, lowercase letters/digits/dashes). "
                            "Auto-derived from name if omitted. Becomes part of the webhook URL."
                        ),
                    },
                    "task_id": {
                        "type": "string",
                        "description": (
                            "Optional. ID of an existing task to run on fire. "
                            "MUST have task_type='trigger' AND match this trigger's "
                            "scope/agent/owner. Use create_one_time_task with "
                            "task_type='trigger' to make one."
                        ),
                    },
                    "notify": {
                        "type": "object",
                        "description": (
                            "Optional inline notification config. Fires alongside (or instead of) "
                            "the linked task. Title and body support `{{placeholder}}` "
                            "substitution from the webhook payload."
                        ),
                        "properties": {
                            "enabled": {"type": "boolean", "default": True},
                            "severity": {
                                "type": "string",
                                "enum": ["info", "success", "warning", "danger"],
                                "default": "info",
                            },
                            "title": {"type": "string", "description": "Title with {{placeholders}}"},
                            "body": {"type": "string", "description": "Body with {{placeholders}}"},
                            "target_scope": {
                                "type": "string",
                                "enum": ["user", "agent", "global"],
                                "description": (
                                    "Recipient scope. For user-scoped triggers only 'user' is "
                                    "valid (and target is locked to creator). For agent-scoped, "
                                    "'agent' broadcasts to all agent users; 'user' notifies a "
                                    "specific user (target = username)."
                                ),
                            },
                            "target": {
                                "type": "string",
                                "description": (
                                    "Username for target_scope='user' or agent name for "
                                    "target_scope='agent'. Omit to default to creator (user-scope) "
                                    "or this agent (agent-scope)."
                                ),
                            },
                        },
                    },
                    "debounce_seconds": {
                        "type": "integer",
                        "default": 0,
                        "description": (
                            "Minimum seconds between fires (rate limit). Useful for chatty "
                            "webhooks like 'commit pushed'. 0 = no debounce."
                        ),
                    },
                    "subscription_id": {
                        "type": "string",
                        "description": (
                            "Optional. ID of a vendor webhook subscription (from "
                            "list_subscriptions). When set, this trigger fires ONLY "
                            "when the subscription receives a matching event — not "
                            "from a generic webhook URL. Scope must match: "
                            "user-scope subscription with user-scope trigger; "
                            "service-scope subscription with agent-scope trigger. "
                            "IMPORTANT: before creating a vendor-subscribed trigger, "
                            "call list_triggers() and check if one already exists with "
                            "the same subscription_id + same event_filter + same "
                            "intended action (task_id or notify). If so, do not create "
                            "a duplicate — use the existing trigger or ask the user "
                            "which one to keep. The server returns a `warnings` array "
                            "in the response when duplicates slip through; report any "
                            "warnings back to the user verbatim."
                        ),
                    },
                    "event_filter": {
                        "type": "object",
                        "description": (
                            "Optional equality dict matched against the normalized "
                            "vendor event. Examples: "
                            "{\"event_type\": \"pull_request\"} fires on every PR event; "
                            "{\"event_type\": \"pull_request\", \"subject.type\": \"opened\"} "
                            "fires only on PR opens; "
                            "{\"subject.type\": [\"opened\", \"reopened\"]} fires on opens "
                            "and reopens. Empty/omitted = fire on every event from the "
                            "subscription. Valid keys: event_type, vendor_event_id, "
                            "actor.{id,email,name,url}, subject.{type,id,title,url}, "
                            "target.{type,id,url}. "
                            "IMPORTANT: event_type is the event CATEGORY and must be one "
                            "of the subscription's events (the `events=` list from "
                            "list_subscriptions — e.g. 'Comment' or 'Issue' for Linear, "
                            "'issue_comment' for GitHub) — do NOT guess values like "
                            "'comment'/'comment.create'; an event_type the subscription "
                            "doesn't receive is REJECTED (it could never fire). "
                            "subject.type is the per-event ACTION (e.g. 'create', "
                            "'opened', 'removed'), NOT the resource. After a test action, "
                            "vendor webhook delivery is async — wait ~10-15s before "
                            "checking fired_count."
                        ),
                    },
                },
                "required": ["name"],
            },
        ),
        Tool(
            name="list_subscriptions",
            description=(
                "List vendor webhook subscriptions the caller has access to. Each "
                "entry includes the subscription_id (use in create_trigger), "
                "provider (github / slack / linear / microsoft / zoom), "
                "vendor_target (e.g. GitHub repo or Slack channel), the events the "
                "subscription receives, status (active / failed / renew_failed), "
                "and the manifest's event_catalog so you know which event_type "
                "values are valid for event_filter. Subscriptions are CREATED via "
                "the OtoDock dashboard (Connected Accounts → Subscribe to events) — "
                "this tool is READ-ONLY."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "provider": {
                        "type": "string",
                        "description": "Filter by provider_id (github, slack, linear, ...)",
                    },
                    "account_label": {
                        "type": "string",
                        "description": "Filter by bound OAuth account label",
                    },
                },
            },
        ),
        Tool(
            name="list_triggers",
            description=(
                "List triggers visible to the current user. By default returns own "
                "user-scoped triggers + all agent-scoped triggers for this agent. "
                "Each row includes the webhook path, status (active/paused), "
                "scope, linked task name, last_fired_at, and fired_count."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "scope": {
                        "type": "string",
                        "enum": AVAILABLE_SCOPES,
                        "description": "Filter to one scope only",
                    },
                },
            },
        ),
        Tool(
            name="get_trigger",
            description="Get a single trigger by id (full detail including webhook URL).",
            inputSchema={
                "type": "object",
                "properties": {"id": {"type": "string"}},
                "required": ["id"],
            },
        ),
        Tool(
            name="edit_trigger",
            description=(
                "Edit an existing trigger without deleting and recreating. Pass only "
                "the fields to change. Scope, slug, agent, and creator are immutable. "
                "Static triggers (loaded from triggers.json) cannot be edited."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "id": {"type": "string"},
                    "name": {"type": "string"},
                    "task_id": {"type": "string"},
                    "notify_enabled": {"type": "boolean"},
                    "notify_severity": {
                        "type": "string",
                        "enum": ["info", "success", "warning", "danger"],
                    },
                    "notify_title": {"type": "string"},
                    "notify_body": {"type": "string"},
                    "notify_target_scope": {
                        "type": "string",
                        "enum": ["user", "agent", "global"],
                    },
                    "notify_target": {"type": "string"},
                    "debounce_seconds": {"type": "integer"},
                    "event_filter": {
                        "type": "object",
                        "description": (
                            "Vendor-trigger event filter (see create_trigger). "
                            "Pass empty {} to match every event from the linked "
                            "subscription."
                        ),
                    },
                },
                "required": ["id"],
            },
        ),
        Tool(
            name="pause_trigger",
            description=(
                "Pause a trigger (sets enabled=FALSE). Webhook fires return 404 "
                "until resumed. The webhook URL stays valid, just inactive."
            ),
            inputSchema={
                "type": "object",
                "properties": {"id": {"type": "string"}},
                "required": ["id"],
            },
        ),
        Tool(
            name="resume_trigger",
            description="Resume a paused trigger.",
            inputSchema={
                "type": "object",
                "properties": {"id": {"type": "string"}},
                "required": ["id"],
            },
        ),
        Tool(
            name="delete_trigger",
            description=(
                "Permanently delete a trigger. The webhook URL becomes 404 "
                "immediately. Static triggers cannot be deleted via this tool."
            ),
            inputSchema={
                "type": "object",
                "properties": {"id": {"type": "string"}},
                "required": ["id"],
            },
        ),
        Tool(
            name="fire_trigger",
            description=(
                "Fire-test a trigger from inside a chat (no Bearer auth needed — "
                "uses session). Useful to verify the trigger config before "
                "configuring the external system. Pass a sample webhook body to "
                "test placeholder substitution."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "id": {"type": "string"},
                    "body": {
                        "type": "object",
                        "description": "Sample webhook payload (used for {{placeholder}} substitution)",
                    },
                },
                "required": ["id"],
            },
        ),
    ]


# =====================================================================
# Tool dispatch
# =====================================================================


@server.call_tool()
async def call_tool(name: str, arguments: dict) -> list[TextContent]:
    try:
        if name == "create_trigger":
            return await _handle_create(arguments)
        if name == "list_triggers":
            return await _handle_list(arguments)
        if name == "list_subscriptions":
            return await _handle_list_subscriptions(arguments)
        if name == "get_trigger":
            return await _handle_get(arguments)
        if name == "edit_trigger":
            return await _handle_edit(arguments)
        if name == "pause_trigger":
            return await _handle_pause(arguments)
        if name == "resume_trigger":
            return await _handle_resume(arguments)
        if name == "delete_trigger":
            return await _handle_delete(arguments)
        if name == "fire_trigger":
            return await _handle_fire(arguments)
        return [TextContent(type="text", text=f"Unknown tool: {name}")]
    except RuntimeError as e:
        # API errors: surface message verbatim
        return [TextContent(type="text", text=f"Error: {e}")]
    except Exception as e:
        return [TextContent(type="text", text=f"Unexpected error: {type(e).__name__}: {e}")]


def _default_scope() -> str:
    """v3 default-scope resolver — same chain across all scope-aware MCPs."""
    return (
        os.environ.get("OTO_DEFAULT_SCOPE")
        or os.environ.get("PROXY_TASK_SCOPE")
        or os.environ.get("OTO_SCOPE")
        or "user"
    )


# visibility-modes: the agent's mode scopes (Personal-only → ["user"], Shared-only
# → ["agent"], collaborative → both). Filters the plain `scope` arg's enum. NOTE:
# the `target_scope` / `notify_target_scope` enums keep "global" (an admin
# broadcast, not a session scope) and are deliberately NOT filtered.
AVAILABLE_SCOPES = [
    s for s in (os.environ.get("OTO_AVAILABLE_SCOPES", "") or "").split(":")
    if s in ("user", "agent")
] or ["user", "agent"]
SCOPE_DEFAULT = _default_scope() if _default_scope() in AVAILABLE_SCOPES else AVAILABLE_SCOPES[0]


async def _handle_create(args: dict) -> list[TextContent]:
    payload = {
        "name": args.get("name", ""),
        "scope": args.get("scope") or _default_scope(),
        "agent": AGENT,
        "debounce_seconds": int(args.get("debounce_seconds") or 0),
    }
    if slug := args.get("slug"):
        payload["slug"] = slug
    if task_id := args.get("task_id"):
        payload["task_id"] = task_id
    # vendor-subscription linkage.
    if sid := args.get("subscription_id"):
        payload["subscription_id"] = sid
    if (ef := args.get("event_filter")) is not None:
        payload["event_filter"] = ef
    notify = args.get("notify") or {}
    if notify.get("enabled") or notify.get("title") or notify.get("body"):
        payload["notify"] = {
            "enabled": notify.get("enabled", True),
            "severity": notify.get("severity") or "info",
            "title": notify.get("title"),
            "body": notify.get("body"),
            "target_scope": notify.get("target_scope"),
            "target": notify.get("target"),
        }
    result = await _post("/v1/triggers", payload)
    trig = result.get("trigger", {})
    is_vendor = bool(trig.get("subscription_id"))
    if is_vendor:
        summary = (
            f"Vendor-subscribed trigger created: '{trig.get('name')}' "
            f"(id={trig.get('id', '')}, scope={trig.get('scope')}, "
            f"subscription_id={trig.get('subscription_id', '')}). "
            f"Fires when the linked subscription receives a matching event "
            f"(filter={trig.get('event_filter') or '{}'})."
        )
    else:
        webhook_path = trig.get("webhook_path") or ""
        summary = (
            f"Trigger created: '{trig.get('name')}' (id={trig.get('id', '')}, "
            f"scope={trig.get('scope')}, slug={trig.get('slug')}). "
            f"Webhook: POST {webhook_path}. "
            f"The user must create an API key from the dashboard "
            f"({'User Settings → API Keys' if trig.get('scope') == 'user' else f'Agent Settings → API Keys for {AGENT}'}) "
            f"and pass it as `Authorization: Bearer otok_…` from the external system."
        )
    # Surface server-side soft warnings (e.g. functionally-duplicate
    # vendor-subscribed trigger detected). The agent reports these back
    # to the user so they decide whether to delete one.
    warnings = result.get("warnings") or []
    if warnings:
        summary += "\n\n⚠️ Warnings:\n" + "\n".join(f"  - {w}" for w in warnings)
    return [TextContent(type="text", text=summary)]


async def _handle_list_subscriptions(args: dict) -> list[TextContent]:
    """Read-only listing of vendor webhook subscriptions the caller can see."""
    params: dict = {}
    if provider := args.get("provider"):
        params["provider_id"] = provider
    if mcp := args.get("mcp_name"):
        params["mcp_name"] = mcp
    result = await _get("/v1/subscriptions", params=params)
    rows = result.get("subscriptions", [])
    if (al := args.get("account_label")):
        rows = [r for r in rows if r.get("account_label") == al]
    if not rows:
        return [TextContent(
            type="text",
            text=("No webhook subscriptions yet. Subscriptions are created in "
                  "the OtoDock dashboard: Connected Accounts → expand an account → "
                  "Subscribe to events. Then come back and link triggers to them "
                  "via create_trigger(subscription_id=...).")
        )]
    lines = [f"Webhook subscriptions ({len(rows)}):"]
    for r in rows:
        events = ", ".join(r.get("selected_events") or [])
        lines.append(
            f"  • {r.get('provider_id')}/{r.get('account_label')} → "
            f"{r.get('vendor_target')}  events={events or '—'}  "
            f"status={r.get('status')}  id={r.get('id', '')}"
        )
    return [TextContent(type="text", text="\n".join(lines))]


async def _handle_list(args: dict) -> list[TextContent]:
    params: dict = {"agent": AGENT}
    if scope := args.get("scope"):
        params["scope"] = scope
    result = await _get("/v1/triggers", params=params)
    rows = result.get("triggers", [])
    if not rows:
        return [TextContent(type="text", text="No triggers configured for this agent.")]
    lines = [f"Triggers for {AGENT} ({len(rows)}):"]
    for r in rows:
        status = "active" if r.get("enabled") else "paused"
        scope = r.get("scope")
        creator = r.get("created_by_name") or r.get("created_by", "?")
        scope_label = f"{scope}/{creator}" if scope == "user" else scope
        last = r.get("last_fired_at") or "never"
        actions = []
        if r.get("task_id"):
            actions.append(f"task={r.get('task_name') or r['task_id']}")
        if r.get("notify_enabled"):
            actions.append(f"notify({r.get('notify_severity')})")
        action_str = " + ".join(actions) or "—"
        lines.append(
            f"  • {r.get('slug')} ({r.get('name')}) "
            f"[{scope_label}] [{status}] fires={r.get('fired_count', 0)} "
            f"last={last} action={action_str} id={r.get('id', '')}"
        )
    return [TextContent(type="text", text="\n".join(lines))]


async def _handle_get(args: dict) -> list[TextContent]:
    trigger_id = args.get("id", "")
    if not trigger_id:
        return [TextContent(type="text", text="Error: id required")]
    result = await _get(f"/v1/triggers/{trigger_id}")
    return [TextContent(type="text", text=json.dumps(result, indent=2, default=str))]


async def _handle_edit(args: dict) -> list[TextContent]:
    trigger_id = args.pop("id", "")
    if not trigger_id:
        return [TextContent(type="text", text="Error: id required")]
    # Filter to known editable fields. `event_filter` is editable for
    # vendor-subscribed triggers; subscription_id and slug remain
    # immutable once set.
    editable = {
        "name", "task_id",
        "notify_enabled", "notify_severity", "notify_title", "notify_body",
        "notify_target_scope", "notify_target", "debounce_seconds",
        "event_filter",
    }
    payload = {k: v for k, v in args.items() if k in editable}
    if not payload:
        return [TextContent(type="text", text="Error: no editable fields supplied")]
    await _post(f"/v1/triggers/{trigger_id}/edit", payload)
    return [TextContent(type="text", text=f"Trigger {trigger_id} updated.")]


async def _handle_pause(args: dict) -> list[TextContent]:
    tid = args.get("id", "")
    await _post(f"/v1/triggers/{tid}/pause", {})
    return [TextContent(type="text", text=f"Trigger {tid} paused.")]


async def _handle_resume(args: dict) -> list[TextContent]:
    tid = args.get("id", "")
    await _post(f"/v1/triggers/{tid}/resume", {})
    return [TextContent(type="text", text=f"Trigger {tid} resumed.")]


async def _handle_delete(args: dict) -> list[TextContent]:
    tid = args.get("id", "")
    await _delete(f"/v1/triggers/{tid}")
    return [TextContent(type="text", text=f"Trigger {tid} deleted.")]


async def _handle_fire(args: dict) -> list[TextContent]:
    tid = args.get("id", "")
    body = args.get("body") or {}
    result = await _post(f"/v1/triggers/{tid}/fire", body)
    return [TextContent(type="text", text=f"Test fire result:\n{json.dumps(result, indent=2)}")]


# =====================================================================
# Main
# =====================================================================


async def _main():
    from mcp.server.stdio import stdio_server
    async with stdio_server() as (reader, writer):
        await server.run(reader, writer, server.create_initialization_options())


if __name__ == "__main__":
    asyncio.run(_main())
