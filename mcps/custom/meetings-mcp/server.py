"""Meetings MCP — multi-agent meeting collaboration tools."""

import asyncio
import json
import os

import httpx
from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import TextContent, Tool, ToolAnnotations

from _tool_args import ToolArgs

server = Server("meetings-mcp")
TOOL_ARGS = ToolArgs()

AGENT = os.environ.get("MEETINGS_MCP_AGENT", "system-admin")
PROXY_URL = os.environ.get("PROXY_URL", "http://localhost:8400").rstrip("/")
API_KEY = os.environ.get("MEETINGS_MCP_API_KEY") or os.environ.get("PROXY_API_KEY", "")


def _headers() -> dict:
    return {
        "Authorization": f"Bearer {API_KEY}",
        "X-Agent-Name": AGENT,
        "Content-Type": "application/json",
    }


async def _get(path: str, params: dict | None = None) -> dict:
    async with httpx.AsyncClient(timeout=30.0) as client:
        resp = await client.get(f"{PROXY_URL}{path}", params=params, headers=_headers())
        resp.raise_for_status()
        return resp.json()


async def _post(path: str, body: dict, headers: dict | None = None) -> dict:
    async with httpx.AsyncClient(timeout=30.0) as client:
        resp = await client.post(f"{PROXY_URL}{path}", json=body, headers=headers or _headers())
        resp.raise_for_status()
        return resp.json()


async def _get_current_session_info() -> tuple[str | None, str | None]:
    """Resolve the calling session's routing anchors ``(session_id, chat_id)``.

    For task contexts, use env vars (task sessions are excluded from
    /v1/session/current, which would return the wrong chat). Identity is NOT
    fetched — the proxy attributes the meeting creator from the session token.
    """
    env_chat = os.environ.get("MEETINGS_MCP_CHAT_ID")
    if env_chat:
        return (
            os.environ.get("MEETINGS_MCP_SESSION_ID") or os.environ.get("OTO_SESSION_ID"),
            env_chat,
        )
    try:
        result = await _get("/v1/session/current")
        return result.get("session_id"), result.get("chat_id")
    except (httpx.HTTPError, ValueError):
        return None, None


@server.list_tools()
async def list_tools() -> list[Tool]:
    return TOOL_ARGS.listed([
        Tool(
            name="start_meeting",
            description=(
                "Start a multi-agent meeting. Invites the specified agents to a "
                "collaborative discussion on the given topic. Use direct_to() during "
                "the meeting to address specific agents — they will respond in parallel "
                "if multiple are addressed. Read the skill `meetings-guide` (Skill tool) "
                "before your first meeting: the turn mechanics and the moderator rules."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "topic": {
                        "type": "string",
                        "description": "The meeting topic or agenda. Be specific about what you want to discuss.",
                    },
                    # anyOf, not a strict array: with deferred tool schemas
                    # (CLI tool search), agents call this BLIND before the
                    # schema is in context and emit `agents` as a string —
                    # the CLI validates against THIS schema and bounces the
                    # call, costing a failed-call -> ToolSearch -> retry loop
                    # per use. Accept the string shapes; the handler
                    # auto-parses them (JSON-encoded array / bare slug /
                    # comma list).
                    "agents": {
                        "anyOf": [
                            {"type": "array", "items": {"type": "string"}},
                            {"type": "string"},
                        ],
                        "description": (
                            "List of agent slugs to invite (array preferred; a "
                            "JSON-encoded array or comma-separated string is also "
                            "accepted). You will be included automatically as moderator."
                        ),
                    },
                    "max_turns": {
                        "type": "integer",
                        "description": "Maximum total turns across all agents (default 30).",
                        "default": 30,
                    },
                },
                "required": ["topic", "agents"],
            },
            annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False, openWorldHint=False),
        ),
        Tool(
            name="direct_to",
            description=(
                "Direct your current response to specific agents. Those agents will "
                "speak next (in parallel if multiple). If you don't call this tool, "
                "your response is broadcast to all participants. All responses are "
                "visible to everyone in the transcript regardless of routing. "
                "Only the response TEXT you wrote before this call is relayed — "
                "the other agents never see your tool results or thinking, so "
                "write your findings out as text FIRST, then call this. "
                "Calling it ENDS your turn: stop immediately after it (no more "
                "tools, no more text). The addressed agents speak only after your "
                "response ends, and their replies reach you in your NEXT turn — "
                "never wait for them, poll for them, or look them up with other "
                "tools; later tool calls in this turn are denied."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    # anyOf for the same deferred-schema reason as start_meeting.
                    "agents": {
                        "anyOf": [
                            {"type": "array", "items": {"type": "string"}},
                            {"type": "string"},
                        ],
                        "description": (
                            "Agent slugs to address (array preferred; a JSON-encoded "
                            "array or comma-separated string is also accepted). They "
                            "will respond next."
                        ),
                    },
                },
                "required": ["agents"],
            },
            annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False, openWorldHint=False),
        ),
        Tool(
            name="end_meeting",
            description=(
                "End the meeting. Only the moderator can use this. Your current "
                "response is the final message: write the summary as response "
                "text BEFORE calling this, then stop (no more tools). No further "
                "turns will happen; this session closes when your response ends."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "meeting_id": {
                        "type": "string",
                        "description": "The meeting ID to end.",
                    },
                },
                "required": ["meeting_id"],
            },
            annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False, openWorldHint=False),
        ),
        Tool(
            name="propose_conclude",
            description=(
                "Propose ending the meeting. The meeting pauses and the moderator "
                "decides whether to conclude or continue. Use when you believe the "
                "discussion has reached its natural end."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "meeting_id": {
                        "type": "string",
                        "description": "The meeting ID.",
                    },
                },
                "required": ["meeting_id"],
            },
            annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False, openWorldHint=False),
        ),
        Tool(
            name="leave_meeting",
            description=(
                "Leave an active meeting. Use when the topic is outside your "
                "expertise or you have nothing more to contribute."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "meeting_id": {
                        "type": "string",
                        "description": "The meeting ID to leave.",
                    },
                    "reason": {
                        "type": "string",
                        "description": "Optional reason for leaving.",
                        "default": "",
                    },
                },
                "required": ["meeting_id"],
            },
            annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False, openWorldHint=False),
        ),
    ])


@server.call_tool(validate_input=False)
async def call_tool(name: str, arguments: dict) -> list[TextContent]:
    arguments, refusal = TOOL_ARGS.check(name, arguments)
    if refusal is not None:
        return refusal
    try:
        if name == "start_meeting":
            session_id, chat_id = await _get_current_session_info()

            topic = arguments["topic"]
            agents = arguments["agents"]
            # LLMs sometimes pass arrays as JSON strings — auto-parse (same
            # tolerance as direct_to below; a raw string here used to
            # TypeError on `[AGENT] + agents`, erroring the tool call and
            # burning an expensive retry loop until the model produced a
            # real array).
            if isinstance(agents, str):
                try:
                    agents = json.loads(agents)
                except (json.JSONDecodeError, TypeError):
                    agents = [a.strip() for a in agents.split(",") if a.strip()]
            if isinstance(agents, str):
                agents = [agents]
            max_turns = arguments.get("max_turns", 30)

            if AGENT not in agents:
                agents = [AGENT] + agents

            scope = (
                os.environ.get("PROXY_TASK_SCOPE")
                or os.environ.get("OTO_DEFAULT_SCOPE")
                or os.environ.get("OTO_SCOPE")
                or "user"
            )
            result = await _post("/v1/meetings", {
                "topic": topic,
                "agents": agents,
                "max_turns": max_turns,
                "parent_chat_id": chat_id or "",
                "parent_session_id": session_id,
                "scope": scope,
            })

            meeting_id = result["meeting_id"]
            await _post(f"/v1/meetings/{meeting_id}/start", {})

            agent_list = ", ".join(agents)
            return [TextContent(
                type="text",
                text=(
                    f"Meeting started (ID: {meeting_id}).\n"
                    f"Topic: {topic}\n"
                    f"Participants: {agent_list}\n"
                    f"Max turns: {max_turns}\n\n"
                    f"Meeting is being set up. You will receive a dedicated prompt "
                    f"as moderator to open the discussion."
                ),
            )]

        elif name == "direct_to":
            agents = arguments.get("agents", [])
            # LLMs sometimes pass arrays as JSON strings — auto-parse
            if isinstance(agents, str):
                try:
                    agents = json.loads(agents)
                except (json.JSONDecodeError, TypeError):
                    agents = [agents]  # treat as single agent slug
            # This tool is a signal — the orchestrator detects it from the event stream.
            # No API call needed. The result text is what the model reads next,
            # so it carries the turn rule: the orchestrator routes only when the
            # turn ends, and the replies arrive in the NEXT turn.
            if agents:
                return [TextContent(type="text", text=(
                    f"Response directed to: {', '.join(agents)}. Your turn is over — "
                    f"stop now (no more tools, no more text). They speak after your "
                    f"response ends; their replies reach you in your next turn."
                ))]
            else:
                return [TextContent(type="text", text=(
                    "Response directed to chat (no agents queued). Your turn is "
                    "over — stop now."
                ))]

        elif name == "end_meeting":
            meeting_id = arguments["meeting_id"]
            await _post(f"/v1/meetings/{meeting_id}/end", {})
            return [TextContent(type="text", text=(
                f"Meeting {meeting_id} is ending; this is the final turn. If the "
                f"meeting summary is not already written above, write it now as "
                f"plain response text, then stop. No further tools."
            ))]

        elif name == "propose_conclude":
            meeting_id = arguments["meeting_id"]
            await _post(f"/v1/meetings/{meeting_id}/propose-conclude", {})
            return [TextContent(type="text", text=(
                f"Conclusion proposed for meeting {meeting_id}. The moderator will "
                f"decide in their turn. Your turn is over — stop now."
            ))]

        elif name == "leave_meeting":
            meeting_id = arguments["meeting_id"]
            reason = arguments.get("reason", "")
            await _post(f"/v1/meetings/{meeting_id}/leave", {"reason": reason})
            return [TextContent(type="text", text=(
                f"You have left meeting {meeting_id}. Your turn is over — stop now."
            ))]

        else:
            return [TextContent(type="text", text=f"Unknown tool: {name}")]

    except httpx.HTTPStatusError as e:
        body = e.response.text[:300]
        return [TextContent(type="text", text=f"API error {e.response.status_code}: {body}")]
    except Exception as e:  # noqa: BLE001  # the tool answers with the error text whatever failed
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
