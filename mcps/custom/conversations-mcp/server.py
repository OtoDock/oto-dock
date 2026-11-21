"""MCP server for the agent's conversations with outside people and on its
own phone line.

Two kinds of session use it, and the platform says which (``GET
/v1/conversations/kind`` with the session's own token, auto-injected as
``PROXY_API_KEY``):

- a person's own session with the editor role or above on the agent reads
  the conversations: ``list_conversations``, ``read_conversation``,
  ``list_people``;
- the wrap-up after a conversation records its outcome and follow-ups:
  ``record_outcome``.

Every other session gets no tool. The platform checks the token on every
call; the list here only spares a model the tools it could not use.
"""

import json
import logging
import os
from urllib.parse import quote

import httpx
from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import TextContent, Tool, ToolAnnotations

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("conversations-mcp")

PROXY_URL = os.environ.get("PROXY_URL", "http://127.0.0.1:8400")
PROXY_API_KEY = os.environ.get("PROXY_API_KEY", "")
_AUTH_HEADERS = {"Authorization": f"Bearer {PROXY_API_KEY}"} if PROXY_API_KEY else {}
_BASE = f"{PROXY_URL}/v1/conversations"

READER = "reader"
WRAPUP = "wrapup"

server = Server("conversations")
_kind: str | None = None


async def _session_kind() -> str:
    """What this session is to the platform, asked once."""
    global _kind
    if _kind is None:
        try:
            async with httpx.AsyncClient(timeout=15.0) as client:
                resp = await client.get(f"{_BASE}/kind", headers=_AUTH_HEADERS)
            _kind = str(resp.json().get("kind") or "") if resp.status_code == 200 else ""
        except (httpx.HTTPError, ValueError) as e:
            logger.warning("the platform did not say what this session is: %s", e)
            return ""
    return _kind


_READ = ToolAnnotations(readOnlyHint=True, destructiveHint=False, openWorldHint=False)

_READER_TOOLS = [
    Tool(
        name="list_conversations",
        description=(
            "The agent's conversations with outside people and on its own phone line, "
            "the most recent first: when, which line, who (an outside person's id), "
            "the outcome and the follow-ups recorded after it, and the person's first "
            "words. Use it to answer what people asked or how calls went."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "days": {"type": "integer", "description": "How many days back, 7 when left out.",
                         "minimum": 1, "maximum": 365},
                "person": {"type": "string",
                           "description": "One outside person's id, from list_people."},
                "limit": {"type": "integer", "description": "At most this many, 50 when left out.",
                          "minimum": 1, "maximum": 200},
            },
        },
        annotations=_READ,
    ),
    Tool(
        name="read_conversation",
        description=(
            "One conversation's words, the person's and the agent's, with its outcome "
            "and follow-ups. Tool results are left out."
        ),
        inputSchema={
            "type": "object",
            "properties": {"id": {"type": "string", "description": "The conversation's id."}},
            "required": ["id"],
        },
        annotations=_READ,
    ),
    Tool(
        name="list_people",
        description=(
            "The outside people who reached the agent: each one's id, channel and "
            "number, when they were first and last seen, whether a PIN verified them, "
            "how many conversations, and the last outcome."
        ),
        inputSchema={"type": "object", "properties": {}},
        annotations=_READ,
    ),
]

_WRAPUP_TOOLS = [
    Tool(
        name="record_outcome",
        description=(
            "Record how the conversation ended: the outcome in one sentence and the "
            "follow-ups, each a short line. Call it once, at the end. A note for the "
            "team is one line about something the whole team should know, kept only "
            "when this agent keeps notes from conversations."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "outcome": {"type": "string", "description": "The outcome, one sentence."},
                "follow_ups": {"type": "array", "items": {"type": "string"},
                               "description": "What someone should do next, each a short line."},
                "note": {"type": "string",
                         "description": "One line for the team, or leave it out."},
            },
            "required": ["outcome"],
        },
        annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False, openWorldHint=False),
    ),
]


@server.list_tools()
async def list_tools() -> list[Tool]:
    kind = await _session_kind()
    if kind == READER:
        return _READER_TOOLS
    if kind == WRAPUP:
        return _WRAPUP_TOOLS
    return []


async def _get(path: str, params: dict | None = None) -> str:
    async with httpx.AsyncClient(timeout=30.0) as client:
        resp = await client.get(f"{_BASE}{path}", params=params or {}, headers=_AUTH_HEADERS)
    if resp.status_code != 200:
        return _refusal(resp)
    return json.dumps(resp.json(), ensure_ascii=False, indent=1)


def _refusal(resp: httpx.Response) -> str:
    try:
        detail = resp.json().get("detail") or resp.text
    except ValueError:
        detail = resp.text
    return f"Error {resp.status_code}: {detail}"


@server.call_tool()
async def call_tool(name: str, arguments: dict) -> list[TextContent]:
    arguments = arguments or {}
    try:
        if name == "list_conversations":
            params = {k: arguments[k] for k in ("days", "person", "limit") if arguments.get(k)}
            text = await _get("", params)
        elif name == "read_conversation":
            cid = str(arguments.get("id") or "").strip()
            text = await _get(f"/{quote(cid, safe='')}") if cid else "Error: id is required."
        elif name == "list_people":
            text = await _get("/people")
        elif name == "record_outcome":
            body = {
                "outcome": str(arguments.get("outcome") or "")[:500],
                "follow_ups": [str(f)[:300] for f in (arguments.get("follow_ups") or [])][:20],
                "note": str(arguments.get("note") or "")[:300],
            }
            async with httpx.AsyncClient(timeout=30.0) as client:
                resp = await client.post(f"{_BASE}/record", json=body, headers=_AUTH_HEADERS)
            text = "Recorded." if resp.status_code == 200 else _refusal(resp)
        else:
            text = f"Unknown tool: {name}"
    except httpx.HTTPError as e:
        text = f"Error: the platform did not answer ({e})."
    return [TextContent(type="text", text=text)]


async def main() -> None:
    async with stdio_server() as (read, write):
        await server.run(read, write, server.create_initialization_options())


if __name__ == "__main__":
    import asyncio
    asyncio.run(main())
