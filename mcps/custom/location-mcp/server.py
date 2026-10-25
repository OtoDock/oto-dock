"""Location MCP Server.

Stdio MCP server providing a location tool for Claude Code agents.
When called, requests the user's GPS location from the dashboard via
the proxy's WebSocket bridge.

Env vars (set in per-agent mcp-config.json):
  LOC_MCP_AGENT       - agent name (for X-Agent-Name header)
  PROXY_URL   - proxy base URL (default: http://localhost:8400)
  LOC_MCP_API_KEY     - auth key (falls back to PROXY_API_KEY from process env)
"""

import asyncio
import os

import httpx
from mcp.server import Server
from mcp.types import TextContent, Tool, ToolAnnotations

from _tool_args import ToolArgs

AGENT = os.environ.get("LOC_MCP_AGENT", "personal-assistant")
PROXY_URL = os.environ.get("PROXY_URL", "http://localhost:8400").rstrip("/")
API_KEY = os.environ.get("LOC_MCP_API_KEY") or os.environ.get("PROXY_API_KEY", "")

server = Server("location-mcp")
TOOL_ARGS = ToolArgs()
_client = httpx.AsyncClient(timeout=45)  # 45s > 30s server-side timeout


def _headers() -> dict:
    return {
        "Authorization": f"Bearer {API_KEY}",
        "X-Agent-Name": AGENT,
        "Content-Type": "application/json",
    }


@server.list_tools()
async def list_tools() -> list[Tool]:
    return TOOL_ARGS.listed([
        Tool(
            name="get_user_location",
            description=(
                "Get the user's current GPS location. Requests location from the user's "
                "dashboard (browser or Android app). The user may see a location permission "
                "prompt if they haven't granted it before. Returns latitude, longitude, "
                "accuracy in meters, and an approximate address when this agent has Google "
                "Maps set up. Only works when the user has an active dashboard session."
            ),
            inputSchema={
                "type": "object",
                "properties": {},
                "required": [],
            },
            annotations=ToolAnnotations(readOnlyHint=True, openWorldHint=True),
        ),
    ])


@server.call_tool(validate_input=False)
async def call_tool(name: str, arguments: dict) -> list[TextContent]:
    arguments, refusal = TOOL_ARGS.check(name, arguments)
    if refusal is not None:
        return refusal
    if name != "get_user_location":
        return [TextContent(type="text", text=f"Unknown tool: {name}")]

    try:
        resp = await _client.post(
            f"{PROXY_URL}/v1/location/request",
            json={},
            headers=_headers(),
        )
        resp.raise_for_status()
        result = resp.json()

        if result.get("error"):
            return [TextContent(type="text", text=f"Location error: {result['error']}")]

        lat = result.get("lat")
        lng = result.get("lng")
        accuracy = result.get("accuracy")
        hint = result.get("address_hint", "")

        parts = [
            f"User location: {lat}, {lng}",
            f"Accuracy: {accuracy:.0f} meters" if accuracy else "",
            f"Address: {hint}" if hint else "",
        ]
        return [TextContent(type="text", text="\n".join(p for p in parts if p))]

    except httpx.TimeoutException:
        return [TextContent(
            type="text",
            text="Location request timed out. The user may not have an active dashboard session.",
        )]
    except httpx.HTTPStatusError as e:
        return [TextContent(type="text", text=f"Location request failed: {e.response.text}")]
    except Exception as e:  # noqa: BLE001  # the tool answers with the error text whatever failed
        return [TextContent(type="text", text=f"Error: {e}")]


async def main():
    from mcp.server.stdio import stdio_server
    async with stdio_server() as (read_stream, write_stream):
        await server.run(
            read_stream, write_stream, server.create_initialization_options()
        )


if __name__ == "__main__":
    asyncio.run(main())
