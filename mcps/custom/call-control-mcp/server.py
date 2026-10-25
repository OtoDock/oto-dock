"""MCP server exposing the call tools of a phone session.

``end_call``, ``transfer_call`` and, on a call another agent placed,
``ask_initiating_agent`` reach the platform's ``POST /v1/phone/call-control``
with the session's own token (auto-injected as ``PROXY_API_KEY``); the
platform relays the action to the phone daemon on the call's socket and
answers with the daemon's verdict, which is the tool's text. The platform
attaches this server to every call regardless of assignment.
"""

import logging
import os

import httpx
from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import TextContent, Tool, ToolAnnotations

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("call-control-mcp")

PROXY_URL = os.environ.get("PROXY_URL", "http://127.0.0.1:8400")
PROXY_API_KEY = os.environ.get("PROXY_API_KEY", "")
# "1" on an outbound call another agent placed: the platform resolves the
# manifest's ``${session.call_ask_back}`` into this at session start.
ASK_BACK = os.environ.get("OTO_CALL_ASK_BACK", "") == "1"

_AUTH_HEADERS = {"Authorization": f"Bearer {PROXY_API_KEY}"} if PROXY_API_KEY else {}
_ENDPOINT = f"{PROXY_URL}/v1/phone/call-control"

server = Server("call-control")


@server.list_tools()
async def list_tools() -> list[Tool]:
    tools = [
        Tool(
            name="end_call",
            description=(
                "End the phone call you are on. Say your goodbye in one short sentence "
                "BEFORE calling this; the line closes right after and nothing you write "
                "afterwards is heard. Only during a live call."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "reason": {
                        "type": "string",
                        "description": "Why the call ends, in a few words (for the log).",
                    },
                },
            },
            annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=True, openWorldHint=True),
        ),
        Tool(
            name="transfer_call",
            description=(
                "Hand the caller to one of the transfer targets named in your instructions. "
                "Tell the caller in one short sentence FIRST, then call this with the "
                "target's name; you leave the call. Only during a live call."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "target": {
                        "type": "string",
                        "description": "The target's name (or number) from your instructions.",
                    },
                },
                "required": ["target"],
            },
            annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=True, openWorldHint=True),
        ),
    ]
    if ASK_BACK:
        tools.append(Tool(
            name="ask_initiating_agent",
            description=(
                "Ask the agent that placed this call a question it can answer (a "
                "preference, a fact you lack). Say that you are checking (one short "
                "sentence) and stop: the answer arrives as your next message within "
                "about 40 seconds."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "question": {
                        "type": "string",
                        "description": "The question, in one or two sentences.",
                    },
                },
                "required": ["question"],
            },
            annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False, openWorldHint=False),
        ))
    return tools


_ACTIONS = {
    "end_call": "end",
    "transfer_call": "transfer",
    "ask_initiating_agent": "ask",
}


@server.call_tool()
async def call_tool(name: str, arguments: dict) -> list[TextContent]:
    action = _ACTIONS.get(name)
    if action is None:
        return [TextContent(type="text", text=f"Unknown tool: {name}")]
    arguments = arguments or {}
    body = {"action": action}
    if action == "end":
        body["reason"] = str(arguments.get("reason") or "")[:200]
    elif action == "transfer":
        target = str(arguments.get("target") or "").strip()
        if not target:
            return [TextContent(type="text", text="Error: target is required (a name from your instructions).")]
        body["target"] = target[:80]
    else:
        question = str(arguments.get("question") or "").strip()
        if not question:
            return [TextContent(type="text", text="Error: question is required.")]
        body["question"] = question[:1000]
    try:
        async with httpx.AsyncClient(
            timeout=httpx.Timeout(connect=10.0, read=30.0, write=10.0, pool=10.0),
        ) as client:
            resp = await client.post(_ENDPOINT, json=body, headers=_AUTH_HEADERS)
    except httpx.HTTPError as e:
        return [TextContent(type="text", text=f"Error reaching the platform: {e}")]
    try:
        payload = resp.json()
    except ValueError:
        payload = {}
    if resp.status_code == 200:
        return [TextContent(type="text", text=str(payload.get("message") or "Done."))]
    detail = payload.get("detail") if isinstance(payload, dict) else None
    return [TextContent(type="text", text=f"Not done: {detail or resp.text[:300]}")]


async def main():
    async with stdio_server() as (read_stream, write_stream):
        await server.run(read_stream, write_stream, server.create_initialization_options())


if __name__ == "__main__":
    import asyncio
    asyncio.run(main())
