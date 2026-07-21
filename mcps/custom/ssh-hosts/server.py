"""ssh-hosts MCP — mid-session lookup of the agent's authorized SSH hosts.

The SSH Hosts prompt block is static text rendered once at session build;
long sessions lose it to attention decay and the agent "forgets" which hosts
and keys exist. This minimal stdio server exposes ONE read-only tool,
``list_ssh_hosts``, that re-fetches the same list from the platform on
demand. Actual SSH access stays plain ``ssh``/``scp``/``rsync`` from bash —
there is deliberately no exec-style tool surface here (see README.md).

The server runs where the agent's shell runs (local sandbox or admin-paired
satellite — user-paired machines never get this MCP), so ``platform.system()``
is the right OS for the ControlMaster mux decision, made proxy-side via the
``target_os`` query param.

Env vars (auto-injected by ``core/oto_env.py`` + ``env_builder.py``):
  OTO_AGENT_NAME       — agent slug (used for ``/v1/agents/{slug}/ssh-hosts``)
  PROXY_URL            — proxy base URL
  PROXY_API_KEY        — session-scoped JWT (not the master key)
"""

from __future__ import annotations

import asyncio
import os
import platform
from typing import Any

import httpx
from mcp.server import Server
from mcp.types import TextContent, Tool


AGENT_NAME = os.environ.get("OTO_AGENT_NAME", "")
PROXY_URL = os.environ.get("PROXY_URL", "http://localhost:8400").rstrip("/")
API_KEY = os.environ.get("PROXY_API_KEY", "")

server = Server("ssh-hosts")
_client = httpx.AsyncClient(timeout=10.0)


class _ApiError(RuntimeError):
    """Wrapped HTTP error from the proxy. Message is shown to the LLM verbatim."""


def _target_os() -> str:
    return {"linux": "linux", "darwin": "darwin", "windows": "windows"}.get(
        platform.system().lower(), "linux",
    )


async def _get(path: str, params: dict[str, Any]) -> Any:
    url = f"{PROXY_URL}{path}"
    try:
        r = await _client.get(url, params=params, headers={
            "Authorization": f"Bearer {API_KEY}",
        })
    except httpx.HTTPError as exc:
        raise _ApiError(f"platform unreachable ({exc})") from exc
    if r.status_code != 200:
        detail = ""
        try:
            detail = (r.json() or {}).get("detail", "")
        except ValueError:
            detail = r.text[:200]
        raise _ApiError(f"platform returned {r.status_code}: {detail}")
    return r.json()


_LIST_TOOL = Tool(
    name="list_ssh_hosts",
    description=(
        "List the SSH hosts this agent is authorized to reach, with the key "
        "file each one uses and a ready-to-run ssh command (keys are already "
        "provisioned at $OTO_SSH_KEY_DIR). Call this whenever you are unsure "
        "which hosts or keys are available — the list in the system prompt "
        "and this tool return the same data, but this is always fresh."
    ),
    inputSchema={"type": "object", "properties": {}, "required": []},
)


@server.list_tools()
async def list_tools() -> list[Tool]:
    return [_LIST_TOOL]


async def _handle_list_ssh_hosts(_args: dict) -> str:
    if not AGENT_NAME:
        return "❌ Error: agent context unavailable"
    data = await _get(
        f"/v1/agents/{AGENT_NAME}/ssh-hosts", {"target_os": _target_os()},
    )
    hosts = (data or {}).get("hosts", [])
    if not hosts:
        return "No SSH hosts are configured for this agent."
    lines = [
        f"{len(hosts)} SSH host(s) configured for this agent. Keys are "
        "provisioned at `$OTO_SSH_KEY_DIR`; use plain `ssh`/`scp`/`rsync` "
        "from your shell.\n",
    ]
    for h in hosts:
        target = f"{h['username']}@{h['host']}" if h.get("username") else h["host"]
        key = h.get("key_name") or "(no key — password/agent auth)"
        lines.append(
            f"- **{h['name']}** — {target}:{h['port']} · key: `{key}`\n"
            f"  `{h['command']}`"
        )
    return "\n".join(lines)


@server.call_tool()
async def call_tool(name: str, arguments: dict) -> list[TextContent]:
    if name != "list_ssh_hosts":
        return [TextContent(type="text", text=f"❌ Unknown tool: {name}")]
    try:
        text = await _handle_list_ssh_hosts(arguments or {})
    except _ApiError as exc:
        text = f"❌ Error: {exc}"
    except Exception as exc:  # noqa: BLE001 — last-resort guard
        text = f"❌ Unexpected error: {type(exc).__name__}: {exc}"
    return [TextContent(type="text", text=text)]


async def _main() -> None:
    from mcp.server.stdio import stdio_server
    async with stdio_server() as (reader, writer):
        await server.run(reader, writer, server.create_initialization_options())


if __name__ == "__main__":
    asyncio.run(_main())
