"""MCP server exposing phone call tools for outbound AI calls.

Originates calls, monitors progress, and handles mid-call questions from
the caller agent — via the PROXY's phone relay (``/v1/phone/calls``), which
forwards to the phone daemon. The proxy is reachable from every place a
session runs (loopback locally, the satellite HTTP tunnel on remote
machines — ``PROXY_URL``/``PROXY_API_KEY`` are auto-injected either way),
so calls work without the session machine needing LAN/DNS reachability to
the phone daemon, and the telephony secret stays on the platform.
"""

import os
import logging
import re

import httpx
from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import TextContent, Tool

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("make-call-mcp")

# call_id lands in request PATHS — a strict shape check keeps a crafted value
# ("../..", "#", "?") from steering the URL at any other proxy endpoint under
# this server's credential.
_CALL_ID_RE = re.compile(r"[A-Za-z0-9_-]{1,64}")


def _bad_call_id(call_id: str) -> list[TextContent] | None:
    if _CALL_ID_RE.fullmatch(call_id or ""):
        return None
    return [TextContent(type="text", text=f"Error: invalid call_id: {call_id!r}")]

PROXY_URL = os.environ.get("PROXY_URL", "http://127.0.0.1:8400")
OUTBOUND_ROUTE_ID = os.environ.get("OUTBOUND_ROUTE_ID", "")
PROXY_API_KEY = os.environ.get("PROXY_API_KEY", "")

# Session JWT for the proxy relay (auto-injected for every stdio MCP).
_AUTH_HEADERS = {"Authorization": f"Bearer {PROXY_API_KEY}"} if PROXY_API_KEY else {}
_CALLS_URL = f"{PROXY_URL}/v1/phone/calls"

server = Server("make-call")


def _format_call_result(result: dict) -> str:
    """Format a call result dict as readable markdown."""
    status = result.get("status", "unknown")
    outcome = result.get("outcome", "")
    error = result.get("error", "")
    transcript = result.get("transcript", [])
    duration = result.get("duration_s")
    question_history = result.get("question_history", [])

    parts = [f"## Call Result: {status.upper()}"]

    if outcome:
        parts.append(f"\n**Outcome:** {outcome}")
    if error:
        parts.append(f"\n**Error:** {error}")
    if duration is not None:
        parts.append(f"\n**Duration:** {duration}s")

    if transcript:
        parts.append("\n### Transcript")
        for entry in transcript:
            role = entry.get("role", "unknown").capitalize()
            text = entry.get("text", "")
            parts.append(f"\n**{role}:** {text}")

    if question_history:
        parts.append("\n### Questions & Answers")
        for qa in question_history:
            parts.append(f"\n**Q:** {qa.get('question', '')}")
            parts.append(f"\n**A:** {qa.get('answer', '')}")

    return "\n".join(parts)


@server.list_tools()
async def list_tools() -> list[Tool]:
    return [
        Tool(
            name="make_call",
            description=(
                "Start an outbound phone call using the AI caller agent. "
                "Returns immediately with a call_id — does NOT block until completion. "
                "Use wait_for_call to monitor progress and handle questions."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "phone_number": {
                        "type": "string",
                        "description": (
                            "Phone number to call in international format "
                            "(e.g., '+302101234567'). Must include country code."
                        ),
                    },
                    "task_description": {
                        "type": "string",
                        "description": (
                            "What the caller agent should accomplish on the call "
                            "(e.g., 'Reserve a table for 4 people at 8pm Saturday')."
                        ),
                    },
                    "instructions": {
                        "type": "string",
                        "description": (
                            "Extra instructions for the caller agent "
                            "(e.g., approved alternative times, name for reservation, "
                            "when to use [QUESTION:] to ask back)."
                        ),
                    },
                },
                "required": ["phone_number", "task_description"],
            },
        ),
        Tool(
            name="wait_for_call",
            description=(
                "Long-poll a running call for live updates. Blocks until one of: "
                "(1) the caller agent asks a [QUESTION:] — show the question to the user IMMEDIATELY "
                "and answer with answer_call_question, "
                "(2) the call COMPLETES or FAILS — present final results to user and STOP, "
                "(3) a new transcript entry appears — show the FULL transcript to the user verbatim. "
                "ALWAYS call wait_for_call again after each transcript_update or question response "
                "to keep getting live updates. "
                "STOP only when the result is COMPLETED or FAILED. "
                "IMPORTANT: Always relay the transcript lines to the user exactly as shown — "
                "do NOT summarize or skip them."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "call_id": {
                        "type": "string",
                        "description": "The call_id returned by make_call.",
                    },
                    "timeout": {
                        "type": "number",
                        "description": (
                            "Maximum seconds to wait (default 120, max 360)."
                        ),
                    },
                },
                "required": ["call_id"],
            },
        ),
        Tool(
            name="answer_call_question",
            description=(
                "Answer a pending question from the caller agent during a call. "
                "The caller agent asked a [QUESTION:] and is waiting on hold. "
                "Send the answer so the caller can continue the conversation."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "call_id": {
                        "type": "string",
                        "description": "The call_id of the active call.",
                    },
                    "answer": {
                        "type": "string",
                        "description": (
                            "The answer to pass to the caller agent "
                            "(e.g., 'Yes, 9:30 PM works — accept it', "
                            "'No, decline and end the call')."
                        ),
                    },
                },
                "required": ["call_id", "answer"],
            },
        ),
        Tool(
            name="get_call_status",
            description=(
                "Get the current status of a call without blocking. "
                "Returns status, transcript so far, any pending question, etc."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "call_id": {
                        "type": "string",
                        "description": "The call_id to check.",
                    },
                },
                "required": ["call_id"],
            },
        ),
    ]


@server.call_tool()
async def call_tool(name: str, arguments: dict) -> list[TextContent]:
    if name == "make_call":
        return await _handle_make_call(arguments)
    elif name == "wait_for_call":
        return await _handle_wait_for_call(arguments)
    elif name == "answer_call_question":
        return await _handle_answer_question(arguments)
    elif name == "get_call_status":
        return await _handle_get_status(arguments)
    else:
        return [TextContent(type="text", text=f"Unknown tool: {name}")]


async def _handle_make_call(arguments: dict) -> list[TextContent]:
    """Start an outbound call (non-blocking)."""
    phone_number = arguments.get("phone_number", "")
    task_description = arguments.get("task_description", "")
    instructions = arguments.get("instructions", "")

    if not phone_number or not task_description:
        return [TextContent(
            type="text",
            text="Error: phone_number and task_description are required.",
        )]

    logger.debug(f"Making call to {phone_number}: {task_description}")

    try:
        async with httpx.AsyncClient(
            timeout=httpx.Timeout(connect=10.0, read=30.0, write=10.0, pool=10.0),
        ) as client:
            body = {
                    "phone_number": phone_number,
                    "task_description": task_description,
                    "instructions": instructions,
                    "wait": False,
                }
            if OUTBOUND_ROUTE_ID:
                body["route_id"] = OUTBOUND_ROUTE_ID
            resp = await client.post(
                f"{_CALLS_URL}",
                json=body,
                headers=_AUTH_HEADERS,
            )

            if resp.status_code not in (200, 202):
                return [TextContent(
                    type="text",
                    text=f"Call API error ({resp.status_code}): {resp.text}",
                )]

            result = resp.json()
            call_id = result.get("call_id", "unknown")
            return [TextContent(
                type="text",
                text=(
                    f"Call started. **call_id:** `{call_id}`\n\n"
                    f"Use `wait_for_call` with this call_id to monitor the call "
                    f"and handle any questions from the caller agent."
                ),
            )]

    except httpx.HTTPError as e:
        return [TextContent(
            type="text",
            text=f"Error reaching the phone relay: {e}",
        )]


async def _handle_wait_for_call(arguments: dict) -> list[TextContent]:
    """Long-poll for call events (question, completion, failure, transcript updates)."""
    call_id = arguments.get("call_id", "")
    timeout = min(arguments.get("timeout", 120), 360)

    if not call_id:
        return [TextContent(type="text", text="Error: call_id is required.")]
    if bad := _bad_call_id(call_id):
        return bad

    try:
        async with httpx.AsyncClient(
            timeout=httpx.Timeout(connect=10.0, read=timeout + 30, write=10.0, pool=10.0),
        ) as client:
            resp = await client.get(
                f"{_CALLS_URL}/{call_id}/wait",
                params={"timeout": str(timeout)},
                headers=_AUTH_HEADERS,
            )

            if resp.status_code == 404:
                return [TextContent(type="text", text=f"Error: call {call_id} not found.")]

            result = resp.json()
            event = result.get("event", "unknown")
            call_data = result.get("call", {})

            if event == "question":
                question = result.get("question", "")
                transcript = call_data.get("transcript", [])
                parts = [
                    "## **Question from the phonecall**\n",
                    f"**{question}**\n",
                ]
                if transcript:
                    parts.append("---")
                    parts.append("### Call Transcript So Far")
                    for entry in transcript:
                        role = entry.get("role", "unknown").capitalize()
                        text = entry.get("text", "")
                        parts.append(f"\n**{role}:** {text}")
                    parts.append("")
                parts.append(
                    "---\n"
                    "The caller agent is on hold waiting for your answer. "
                    f"Use `answer_call_question` with call_id `{call_id}` to respond. "
                    "**Answer quickly — the person is waiting on the phone.**"
                )
                return [TextContent(type="text", text="\n".join(parts))]
            elif event in ("completed", "failed"):
                return [TextContent(
                    type="text",
                    text=(
                        _format_call_result(call_data)
                        + "\n\n**Call is finished. Present the results to the user now.**"
                    ),
                )]
            elif event == "transcript_update":
                status = call_data.get("status", "unknown")
                transcript = call_data.get("transcript", [])
                parts = [
                    f"## Live Call Update (status: {status})\n",
                ]
                if transcript:
                    parts.append("### Transcript")
                    for entry in transcript:
                        role = entry.get("role", "unknown").capitalize()
                        text = entry.get("text", "")
                        parts.append(f"**{role}:** {text}")
                    parts.append("")
                else:
                    parts.append("*No transcript yet.*\n")
                parts.append(
                    "Show the transcript above to the user. "
                    f"Call `wait_for_call` again with call_id `{call_id}` "
                    f"to continue monitoring live updates."
                )
                return [TextContent(type="text", text="\n".join(parts))]
            elif event == "timeout":
                status = call_data.get("status", "unknown")
                transcript = call_data.get("transcript", [])
                parts = [
                    f"Wait timed out after {timeout}s. Call status: **{status}**.\n",
                ]
                if transcript:
                    parts.append("### Call Transcript So Far")
                    for entry in transcript:
                        role = entry.get("role", "unknown").capitalize()
                        text = entry.get("text", "")
                        parts.append(f"\n**{role}:** {text}")
                    parts.append("")
                parts.append(
                    "Call `wait_for_call` again to continue monitoring, "
                    "or use `get_call_status` to check."
                )
                return [TextContent(type="text", text="\n".join(parts))]
            else:
                return [TextContent(
                    type="text",
                    text=f"Unexpected event: {event}\n\n{result}",
                )]

    except httpx.TimeoutException:
        return [TextContent(
            type="text",
            text="Wait request timed out. Call may still be in progress.",
        )]
    except httpx.HTTPError as e:
        return [TextContent(
            type="text",
            text=f"Error reaching the phone relay: {e}",
        )]


async def _handle_answer_question(arguments: dict) -> list[TextContent]:
    """Send an answer to a pending caller agent question."""
    call_id = arguments.get("call_id", "")
    answer = arguments.get("answer", "")

    if not call_id or not answer:
        return [TextContent(
            type="text",
            text="Error: call_id and answer are required.",
        )]
    if bad := _bad_call_id(call_id):
        return bad

    try:
        async with httpx.AsyncClient(
            timeout=httpx.Timeout(connect=10.0, read=10.0, write=10.0, pool=10.0),
        ) as client:
            resp = await client.post(
                f"{_CALLS_URL}/{call_id}/answer",
                json={"answer": answer},
                headers=_AUTH_HEADERS,
            )

            if resp.status_code == 404:
                return [TextContent(type="text", text=f"Error: call {call_id} not found.")]
            elif resp.status_code == 409:
                return [TextContent(
                    type="text",
                    text="No pending question for this call (already answered or call ended).",
                )]

            return [TextContent(
                type="text",
                text=(
                    f"Answer delivered to caller agent.\n\n"
                    f"Use `wait_for_call` with call_id `{call_id}` to continue "
                    f"monitoring for further questions or completion."
                ),
            )]

    except httpx.HTTPError as e:
        return [TextContent(
            type="text",
            text=f"Error reaching the phone relay: {e}",
        )]


async def _handle_get_status(arguments: dict) -> list[TextContent]:
    """Get current call status without blocking."""
    call_id = arguments.get("call_id", "")

    if not call_id:
        return [TextContent(type="text", text="Error: call_id is required.")]
    if bad := _bad_call_id(call_id):
        return bad

    try:
        async with httpx.AsyncClient(
            timeout=httpx.Timeout(connect=10.0, read=10.0, write=10.0, pool=10.0),
        ) as client:
            resp = await client.get(
                f"{_CALLS_URL}/{call_id}",
                headers=_AUTH_HEADERS,
            )

            if resp.status_code == 404:
                return [TextContent(type="text", text=f"Error: call {call_id} not found.")]

            result = resp.json()
            return [TextContent(
                type="text",
                text=_format_call_result(result),
            )]

    except httpx.HTTPError as e:
        return [TextContent(
            type="text",
            text=f"Error reaching the phone relay: {e}",
        )]


async def main():
    async with stdio_server() as (read_stream, write_stream):
        await server.run(read_stream, write_stream, server.create_initialization_options())


if __name__ == "__main__":
    import asyncio
    asyncio.run(main())
