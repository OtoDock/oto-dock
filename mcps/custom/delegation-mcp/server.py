"""Delegation MCP Server.

stdio MCP server for visible parallel delegation: spawn worker sessions
(background tasks or first-class chats), monitor sibling sessions, peek
into delegated lanes, and send file copies to a target's inbox
(send_files — the passive half of cross-agent handoff). Scheduling
(recurring/one-time/trigger tasks) lives in the separate schedules-mcp.

Environment variables (set in per-agent mcp-config.json):
  DELEGATION_MCP_AGENT    - Which agent this instance serves (e.g. "system-admin")
  PROXY_URL               - Proxy URL (e.g. "http://localhost:8400")
  DELEGATION_MCP_API_KEY  - Proxy API key (falls back to PROXY_API_KEY from process env)
  DELEGATION_MCP_TARGETS  - Comma-separated cross-agent delegation roster
"""

import asyncio
import os
import re
from pathlib import Path

import httpx
from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import TextContent, Tool

AGENT = os.environ.get("DELEGATION_MCP_AGENT", "system-admin")
PROXY_URL = os.environ.get("PROXY_URL", "http://localhost:8400").rstrip("/")
API_KEY = os.environ.get("DELEGATION_MCP_API_KEY") or os.environ.get("PROXY_API_KEY", "")
DELEGATION_TARGETS = [t.strip() for t in os.environ.get("DELEGATION_MCP_TARGETS", "").split(",") if t.strip()]

# The session's scope context (drives worker-scope inheritance; the proxy
# clamps server-side to the target agent's visibility mode).
PARENT_SCOPE = (
    os.environ.get("PROXY_TASK_SCOPE")
    or os.environ.get("OTO_DEFAULT_SCOPE")
    or os.environ.get("OTO_SCOPE")
    or "user"
)


def _validate_output_dir(output_dir: str) -> str | None:
    """Validate output_dir is a safe relative path. Returns error message or None."""
    if not output_dir:
        return "output_dir cannot be empty"
    if output_dir.startswith("/") or output_dir.startswith("\\"):
        return "output_dir must be a relative path"
    parts = Path(output_dir).parts
    if ".." in parts:
        return "output_dir must not contain '..'"
    if any(p.startswith(".") for p in parts):
        return "output_dir must not contain hidden directories"
    return None


server = Server("delegation-mcp")


def _headers() -> dict:
    return {
        "Authorization": f"Bearer {API_KEY}",
        "X-Agent-Name": AGENT,
        "Content-Type": "application/json",
    }


async def _post(path: str, body: dict) -> dict:
    async with httpx.AsyncClient(timeout=30.0) as client:
        resp = await client.post(f"{PROXY_URL}{path}", json=body, headers=_headers())
        resp.raise_for_status()
        return resp.json()


async def _get(path: str, params: dict | None = None) -> dict:
    async with httpx.AsyncClient(timeout=30.0) as client:
        resp = await client.get(f"{PROXY_URL}{path}", params=params, headers=_headers())
        resp.raise_for_status()
        return resp.json()


_STATUS_LABEL = {
    "generating": "generating",
    "awaiting_user": "awaiting reply",
    "idle": "idle",
}


@server.list_tools()
async def list_tools() -> list[Tool]:
    return [
        Tool(
            name="delegate",
            description=(
                "Delegate work to a parallel worker session and always get the result back.\n\n"
                "This tool returns immediately — the worker runs asynchronously. When its "
                "turn completes, the result is automatically delivered back into THIS "
                "session (including anything the user said to the worker in the meantime). "
                "Continue your current work after calling this — do not wait or poll.\n\n"
                "Choosing the surface (REQUIRED — no default):\n"
                "- 'chat': the worker is a first-class chat the user can SEE, WATCH LIVE, "
                "steer, and continue — visible parallelism. Pick this for user-present "
                "work: coding lanes, drafts the user may want to redirect, anything worth "
                "supervising. If the user interrupts or redirects the worker, you get a "
                "callback with status 'user_interrupted' and their instruction — adapt "
                "instead of assuming the lane died.\n"
                "- 'task': a background task run (visible in the agent's History, not the "
                "chat list). Pick this for fire-and-forget research, bulk processing, and "
                "work nobody needs to watch.\n\n"
                "The worker's result is a REPORT to you, not a conversation. Do not "
                "delegate again just to acknowledge, thank, or confirm it. Delegate a "
                "follow-up (continue_id) only when the user asked for more work or the "
                "worker needs something only you can supply; otherwise fold the result "
                "into your reply to the user and let them decide."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "name": {"type": "string", "description": "Short human-readable name (becomes the worker chat's title for surface=chat)"},
                    "prompt": {"type": "string", "description": "The prompt the worker executes"},
                    "surface": {
                        "type": "string",
                        "enum": ["chat", "task"],
                        "description": (
                            "REQUIRED. 'chat' = visible, steerable worker chat "
                            "(user-present work); 'task' = background task run "
                            "(fire-and-forget work)."
                        ),
                    },
                    "agent": {
                        "type": "string",
                        "description": (
                            f"Agent to run the worker as. Available: {', '.join(DELEGATION_TARGETS)}. Defaults to this agent ({AGENT}). Omit with continue_id — the continued worker's own agent is used."
                            if DELEGATION_TARGETS
                            else "Agent to run the worker as (defaults to this agent). Omit with continue_id — the continued worker's own agent is used."
                        ),
                    },
                    "continue_id": {
                        "type": "string",
                        "description": (
                            "Continue a previous delegation (multi-turn): pass the task_id "
                            "of a previous surface=task delegation, or the chat_id of a "
                            "previous worker (chat or task-run). The worker keeps its chat "
                            "and full context from prior rounds, on its own agent."
                        ),
                    },
                    "output_dir": {
                        "type": "string",
                        "description": (
                            "Relative path within the target agent's workspace/ directory "
                            "where the worker should save output files (e.g. 'research/AAPL', "
                            "'reports/monthly'). The worker will be instructed to write "
                            "files there."
                        ),
                    },
                    "project_id": {
                        "type": "string",
                        "description": (
                            "Optional project slug for coordinated multi-lane work (e.g. "
                            "'site-redesign'). Stamps this chat as the project's "
                            "orchestrator and the worker as one of its lanes. Leave unset "
                            "for a plain one-off delegation — no project ceremony needed. "
                            "On a project's FIRST delegate, consider pinning a project "
                            "dashboard (display pin_app with scope='project') so the user "
                            "gets a plan overview beside the live lanes."
                        ),
                    },
                    "model": {
                        "type": "string",
                        "description": (
                            "OPTIONAL model override for this worker — only when the "
                            "task specifically needs a different model (must be "
                            "available on the worker's execution layer). Omit to "
                            "inherit the agent's default. Ignored with continue_id."
                        ),
                    },
                    "layer": {
                        "type": "string",
                        "description": (
                            "OPTIONAL execution-layer override for this worker (e.g. "
                            "'claude-code-cli', 'codex-cli') — must be enabled for the "
                            "agent. Omit to inherit. Ignored with continue_id."
                        ),
                    },
                    "mode": {
                        "type": "string",
                        "enum": ["interactive", "non-interactive"],
                        "description": (
                            "OPTIONAL execution-mode override for this worker: "
                            "'interactive' runs it as a terminal session (steerable "
                            "mid-turn on local PTYs), 'non-interactive' headless. "
                            "Omit to inherit the agent's default. Ignored with "
                            "continue_id."
                        ),
                    },
                },
                "required": ["name", "prompt", "surface"],
            },
        ),
        Tool(
            name="send_files",
            description=(
                "Send COPIES of workspace files to another agent's file inbox. "
                "They land in the target's tree under workspace/inbox/"
                f"{AGENT}/ (existing files are never overwritten); its "
                "sessions see the inbox in their workspace listing, with the "
                "path naming you as the sender. Include a README in `paths` "
                "when the files need context — nothing else is delivered.\n\n"
                "This is the passive half of cross-agent handoff — it does "
                "NOT make the target act or notify it actively. If the target "
                "should process the files NOW, follow up with delegate(...) "
                "and point the prompt at the inbox path."
                + (f"\nTargets: {', '.join(DELEGATION_TARGETS)}." if DELEGATION_TARGETS else "")
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "target_agent": {
                        "type": "string",
                        "description": "Agent to receive the files (must be one of your delegation targets).",
                    },
                    "paths": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": (
                            "Files or directories to send, relative to your "
                            "workspace/ directory (e.g. 'reports/q3.md', "
                            "'research/AAPL'). Directories copy recursively."
                        ),
                    },
                    "dest_dir": {
                        "type": "string",
                        "description": (
                            "Optional subfolder inside your inbox slot on the "
                            "target (e.g. 'q3-review') — relative, no '..'."
                        ),
                    },
                    "note": {
                        "type": "string",
                        "description": (
                            "One-line context recorded on the transfer's audit "
                            "row (admin-visible). NOT delivered to the target — "
                            "context the target needs belongs in the files "
                            "themselves (e.g. a README.md in `paths`)."
                        ),
                    },
                },
                "required": ["target_agent", "paths"],
            },
        ),
        Tool(
            name="list_sessions",
            description=(
                "List the active and recent sessions in your visibility set: your own "
                "chats on this agent (or the shared pool on a shared agent) plus every "
                "worker session this chat delegated. Shows each session's status "
                "(generating / awaiting reply / idle), so you can see what's running "
                "in parallel before delegating more work or peeking into a lane. "
                "With `agent`, extends to what YOUR USER could see in the dashboard "
                "on that agent — their chats there, a shared agent's shared pool, and "
                "the agent's task-run sessions (read-only visibility; it grants no "
                "delegation rights there). No-user sessions can use it on "
                "their wired delegation targets instead (their shared pool + "
                "agent-scope task sessions)."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "limit": {
                        "type": "integer",
                        "minimum": 1,
                        "maximum": 100,
                        "default": 30,
                        "description": "Max sessions to return (default 30).",
                    },
                    "agent": {
                        "type": "string",
                        "description": (
                            "Cross-agent view: an agent slug, or 'all' for every "
                            "agent your user can access. Omit for the default set "
                            "(your own pool + your spawned workers). Sessions "
                            "without a user (scheduled agent-scope runs) can name "
                            "only their wired delegation targets."
                        ),
                    },
                },
            },
        ),
        Tool(
            name="peek_session",
            description=(
                "Peek into another session in your visibility set (a delegated worker, "
                "one of your own chats, or — for user-backed sessions — any session "
                "your user could open in the dashboard, incl. other agents' task-run "
                "sessions from list_sessions(agent=...); no-user sessions can peek "
                "their delegation targets' shared-pool and task sessions): returns its status plus "
                "recent messages, LIVE-inclusive — a session mid-turn shows the "
                "in-progress response so far (partial text + running tools, marked "
                "IN PROGRESS). Use depth to expand to the last N messages."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "chat_id": {"type": "string", "description": "The session's chat id (from list_sessions or a delegate result)"},
                    "depth": {
                        "type": "integer",
                        "minimum": 1,
                        "maximum": 50,
                        "description": "Return the last N user/assistant messages instead of just the final exchange.",
                    },
                },
                "required": ["chat_id"],
            },
        ),
        Tool(
            name="adopt_project",
            description=(
                "Take over an existing delegation project as its orchestrator. "
                "Use this when THIS session picks up a project started elsewhere "
                "(from its board/handoff doc) — it marks this chat as the "
                "project's orchestrator, so the project board and lane overview "
                "appear here and delegate results/monitoring anchor to this "
                "session going forward. The previous orchestrator keeps its "
                "history. Idempotent — safe to call again. Not needed when you "
                "delegate with the project_id yourself (that adopts implicitly)."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "project_id": {
                        "type": "string",
                        "description": "The existing project's slug (from its board file, a delegate result, or list_sessions project markers).",
                    },
                },
                "required": ["project_id"],
            },
        ),
    ]


@server.call_tool()
async def call_tool(name: str, arguments: dict) -> list[TextContent]:
    try:
        if name == "delegate":
            surface = arguments.get("surface")
            if surface not in ("chat", "task"):
                return [TextContent(
                    type="text",
                    text=(
                        "Error: surface is required — pick 'chat' (visible, steerable "
                        "worker chat for user-present work) or 'task' (background run "
                        "for fire-and-forget work)."
                    ),
                )]
            # On continues the proxy derives the agent from the continued
            # worker itself — an explicit value is only a cross-check there.
            # Fresh spawns default to this agent.
            if arguments.get("continue_id"):
                target_agent = arguments.get("agent") or ""
            else:
                target_agent = arguments.get("agent") or AGENT

            # Local roster check (fast feedback; the proxy re-validates).
            if target_agent and target_agent != AGENT:
                if not DELEGATION_TARGETS or target_agent not in DELEGATION_TARGETS:
                    available = ", ".join(DELEGATION_TARGETS) if DELEGATION_TARGETS else "(none configured)"
                    return [TextContent(
                        type="text",
                        text=f"Cannot delegate to '{target_agent}'. Cross-agent delegation targets for this agent: {available}. Self-delegation to '{AGENT}' is always allowed.",
                    )]

            # Validate and resolve output_dir. User-scoped workers → the user's
            # isolated dir; agent-scoped → the shared workspace.
            username = os.environ.get("OTO_USERNAME") or os.environ.get("PROXY_TASK_USERNAME") or ""
            output_dir = arguments.get("output_dir")
            task_prompt = arguments["prompt"]
            if output_dir:
                err = _validate_output_dir(output_dir)
                if err:
                    return [TextContent(type="text", text=f"Invalid output_dir '{output_dir}': {err}")]
                clean_dir = str(Path(output_dir))
                # Continues may omit the agent — the path hint then assumes
                # the common same-agent case (the proxy knows the real one).
                dir_agent = target_agent or AGENT
                if PARENT_SCOPE == "user" and username:
                    full_path = f"agents/{dir_agent}/users/{username}/workspace/{clean_dir}"
                else:
                    full_path = f"agents/{dir_agent}/workspace/{clean_dir}"
                task_prompt = (
                    f"[TASK_OUTPUT_DIR]\n"
                    f"Save any output files to: {full_path}/\n"
                    f"Create the directory if it does not exist.\n"
                    f"[/TASK_OUTPUT_DIR]\n\n"
                    f"{task_prompt}"
                )
            # Worker-side framing (2026-09-03): the final message comes back
            # to the caller as a REPORT. A worker that answers with a to-do
            # list for the caller (which cannot delegate back — chain guard)
            # makes the caller re-open the lane just to reply; two agents then
            # ping-pong at a turn's cost each. Say so up front.
            task_prompt = (
                f"[DELEGATED_WORK]\n"
                f"Delegated by agent '{AGENT}'. When you finish, your final message "
                f"is delivered to that agent automatically as a report — write it as "
                f"one: what you did, what you produced (with paths), and anything "
                f"blocked. Do not send the caller a to-do list or questions to answer "
                f"by delegation: it cannot delegate back to you and would have to "
                f"re-open this lane just to reply. Put anything it must do on a single "
                f"'Blocked on:' line.\n"
                f"[/DELEGATED_WORK]\n\n"
                f"{task_prompt}"
            )

            result = await _post("/v1/delegation/spawn", {
                "name": arguments["name"],
                "prompt": task_prompt,
                "surface": surface,
                "agent": target_agent,
                "source_agent": AGENT,
                "continue_id": arguments.get("continue_id"),
                "project_id": arguments.get("project_id"),
                "scope": PARENT_SCOPE,
                "model": arguments.get("model"),
                "layer": arguments.get("layer"),
                "mode": arguments.get("mode"),
            })

            lines = [
                f"Delegated '{arguments['name']}' (surface={surface}, id: {result['task_id']}, run: {result['run_id']}).",
                f"Agent: {result.get('agent') or target_agent}",
            ]
            if result.get("chat_id"):
                lines.append(f"Worker chat: {result['chat_id']}")
            if result.get("scope_note"):
                lines.append(result["scope_note"])
            if result.get("project_id"):
                lines.append(f"Project: {result['project_id']}")
            lines.append(
                "The result will be delivered to this session automatically when the "
                "worker's turn completes — continue your current work."
            )
            return [TextContent(type="text", text="\n".join(lines))]

        elif name == "send_files":
            target_agent = (arguments.get("target_agent") or "").strip()
            paths = arguments.get("paths") or []
            if not target_agent or not paths:
                return [TextContent(type="text", text="Error: target_agent and a non-empty paths list are required.")]
            # Local fast checks (the proxy re-validates everything).
            if target_agent == AGENT:
                return [TextContent(type="text", text=f"'{AGENT}' is this agent — the files are already in your own tree.")]
            if not DELEGATION_TARGETS or target_agent not in DELEGATION_TARGETS:
                available = ", ".join(DELEGATION_TARGETS) if DELEGATION_TARGETS else "(none configured)"
                return [TextContent(
                    type="text",
                    text=f"Cannot send files to '{target_agent}'. Delegation targets for this agent: {available}.",
                )]
            dest_dir = arguments.get("dest_dir") or ""
            if dest_dir:
                err = _validate_output_dir(dest_dir)
                if err:
                    return [TextContent(type="text", text=f"Invalid dest_dir '{dest_dir}': {err}")]

            result = await _post("/v1/delegation/send_files", {
                "target_agent": target_agent,
                "paths": paths,
                "dest_dir": dest_dir,
                "note": arguments.get("note") or "",
                "source_agent": AGENT,
                "scope": PARENT_SCOPE,
            })

            files = result.get("files") or []
            mb = (result.get("total_bytes") or 0) / (1024 * 1024)
            lines = [
                f"Sent {len(files)} file(s) ({mb:.1f} MB) to '{target_agent}' "
                f"(transfer {result.get('transfer_id', '?')}).",
            ]
            lines.extend(f"  {p}" for p in files[:20])
            if len(files) > 20:
                lines.append(f"  … and {len(files) - 20} more")
            if result.get("scope_note"):
                lines.append(result["scope_note"])
            skipped = result.get("skipped") or []
            if skipped:
                lines.append("Skipped: " + ", ".join(skipped[:10]))
            lines.append(
                "The target's sessions see the inbox in their workspace "
                "listing; it is not actively notified. Use delegate(...) "
                "pointing at the inbox path if it should act on the files now."
            )
            return [TextContent(type="text", text="\n".join(lines))]

        elif name == "list_sessions":
            params = {"limit": arguments.get("limit", 30)}
            if arguments.get("agent"):
                params["agent"] = arguments["agent"]
            result = await _get("/v1/delegation/sessions", params=params)
            sessions = result.get("sessions", [])
            if not sessions:
                return [TextContent(type="text", text="No sessions in your visibility set.")]
            lines = [f"Sessions ({len(sessions)}):"]
            for s in sessions:
                status = _STATUS_LABEL.get(s.get("status", ""), s.get("status", "?"))
                marks = []
                if s.get("is_current"):
                    marks.append("this session")
                if s.get("parent_chat_id"):
                    marks.append("worker")
                if s.get("project_id"):
                    marks.append(f"project:{s['project_id']}")
                mark = f" [{', '.join(marks)}]" if marks else ""
                title = s.get("title") or "(untitled)"
                lines.append(
                    f"  {s['chat_id']} — {title} ({s.get('agent', '?')}, {status}, "
                    f"updated {s.get('updated_at', '—')}){mark}"
                )
            return [TextContent(type="text", text="\n".join(lines))]

        elif name == "peek_session":
            chat_id = arguments["chat_id"]
            # The id lands in the request PATH — a strict shape check keeps a
            # crafted value ("../..", "#", "?") from steering the URL at any
            # other proxy endpoint under this server's credential.
            if not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", chat_id or ""):
                return [TextContent(
                    type="text", text=f"Error: invalid chat_id: {chat_id!r}",
                )]
            params: dict = {}
            if arguments.get("depth"):
                params["depth"] = arguments["depth"]
            result = await _get(f"/v1/delegation/sessions/{chat_id}/peek", params=params)
            status = _STATUS_LABEL.get(result.get("status", ""), result.get("status", "?"))
            lines = [
                f"Session {chat_id} — {result.get('title') or '(untitled)'} "
                f"({result.get('agent', '?')}, {status})",
            ]
            messages = result.get("messages", [])
            if not messages:
                lines.append("(no messages yet)")
            for m in messages:
                who = "User" if m.get("role") == "user" else "Assistant"
                if m.get("in_progress"):
                    who += " — turn IN PROGRESS"
                lines.append(f"\n[{who}]\n{m.get('content', '')}")
            if result.get("truncated"):
                lines.append("\n(older messages omitted — raise depth to see more)")
            return [TextContent(type="text", text="\n".join(lines))]

        elif name == "adopt_project":
            result = await _post("/v1/delegation/adopt", {
                "project_id": arguments["project_id"],
            })
            lines = [
                f"Adopted project '{result['project_id']}' — this session is now "
                f"its orchestrator (board + lane overview anchor here).",
            ]
            lanes = result.get("lanes") or []
            if lanes:
                lines.append("Existing lanes:")
                for lane in lanes:
                    role = f", {lane['delegate_role']}" if lane.get("delegate_role") else ""
                    lines.append(
                        f"  {lane['id']} — {lane.get('title') or '(untitled)'} "
                        f"({lane.get('agent', '?')}{role})"
                    )
            return [TextContent(type="text", text="\n".join(lines))]

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
