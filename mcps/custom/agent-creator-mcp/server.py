"""Agent-Creator-MCP — agents create new agents from template folders.

The agent authors a community-agent template folder somewhere it can write
(its workspace), then hands the path to this server. The proxy loads the
folder through the same pipeline that installs catalog templates, so the
whole manifest surface — persona, required MCPs, skill packages, scheduled
tasks/triggers/notifications, agent-wide and per-user setup guides, auto
context — works identically for a locally authored template.

Permission resolution lives in :func:`_resolve_tool_set` and runs once at
module load using the auto-injected ``OTO_*`` env vars (mirror of
``agent-config-mcp/server.py``):

- platform ``admin`` / ``creator`` → all three tools.
- everyone else (platform ``member``, service sessions with no human
  identity) → zero tools; ``tools/list`` returns an empty list.

The env check is a courtesy that keeps the tools out of a member's prompt.
The real boundary is server side: every endpoint independently requires a
platform creator/admin behind the session JWT.
"""

from __future__ import annotations

import asyncio
import os
from typing import Any

import httpx
from mcp.server import Server
from mcp.types import TextContent, Tool


# ---------------------------------------------------------------------------
# Env + permission matrix
# ---------------------------------------------------------------------------

AGENT_NAME = os.environ.get("OTO_AGENT_NAME", "")
PLATFORM_ROLE = os.environ.get("OTO_PLATFORM_ROLE", "")

PROXY_URL = os.environ.get("PROXY_URL", "http://localhost:8400").rstrip("/")
API_KEY = os.environ.get("PROXY_API_KEY", "")

_ALL_TOOLS = {
    "list_building_blocks",
    "validate_agent_template",
    "create_agent",
}


def _resolve_tool_set() -> set[str]:
    if PLATFORM_ROLE in ("admin", "creator"):
        return set(_ALL_TOOLS)
    # Platform members and sessions with no human identity see nothing.
    return set()


ENABLED_TOOLS = _resolve_tool_set()


# ---------------------------------------------------------------------------
# HTTP helper (mirrors agent-config-mcp)
# ---------------------------------------------------------------------------

class _ApiError(RuntimeError):
    pass


async def _request(method: str, path: str, **kwargs) -> Any:
    headers = kwargs.pop("headers", {}) or {}
    if API_KEY and "Authorization" not in headers:
        headers["Authorization"] = f"Bearer {API_KEY}"
    if AGENT_NAME and "X-Agent-Name" not in headers:
        headers["X-Agent-Name"] = AGENT_NAME
    url = f"{PROXY_URL}{path}"
    # Installs cascade MCP + skill-package downloads — give them room.
    timeout = kwargs.pop("timeout", 120.0)
    last_exc: Exception | None = None
    for attempt in range(2):
        try:
            async with httpx.AsyncClient(timeout=timeout) as client:
                resp = await client.request(method, url, headers=headers, **kwargs)
            if resp.status_code >= 500 and attempt == 0:
                await asyncio.sleep(1.0)
                continue
            if resp.status_code >= 400:
                detail: Any = resp.text
                try:
                    js = resp.json()
                    detail = js.get("detail") or js
                except Exception:
                    pass
                raise _ApiError(f"{method} {path} → {resp.status_code}: {detail}")
            if resp.status_code == 204 or not resp.content:
                return None
            return resp.json()
        except _ApiError:
            raise
        except Exception as exc:
            last_exc = exc
            if attempt == 0:
                await asyncio.sleep(1.0)
                continue
            raise _ApiError(f"{method} {path}: {exc}") from exc
    if last_exc:
        raise _ApiError(f"{method} {path}: {last_exc}")
    return None


# ---------------------------------------------------------------------------
# Tool implementations
# ---------------------------------------------------------------------------

async def _tool_list_building_blocks() -> str:
    data = await _request("GET", "/v1/agents/local-template/building-blocks")
    if not isinstance(data, dict):
        return "❌ Error: unexpected response from building-blocks"

    lines: list[str] = []
    if data.get("catalog_error"):
        lines.append(
            f"> Catalog unreachable ({data['catalog_error']}) — only what is "
            "already installed on this platform is listed below.\n"
        )

    lines.append("# MCPs")
    lines.append(
        "Use the `name` column verbatim in `mcps.json`. Core MCPs are "
        "assigned to every agent automatically — never list them.\n"
    )
    lines.append("| name | installed | category | what it does |")
    lines.append("|---|---|---|---|")
    for m in data.get("mcps", []):
        installed = "yes" if m.get("installed") else "no — admin install needed"
        desc = (m.get("description") or "").replace("|", "/")
        lines.append(
            f"| `{m.get('name', '')}` | {installed} | {m.get('category', '')} | {desc} |",
        )

    skills = data.get("skill_packages", [])
    lines.append("\n# Skill packages")
    if skills:
        lines.append(
            "For `skills.json`. Packages not yet installed can only be "
            "pulled in by a platform admin.\n"
        )
        lines.append("| name | installed | what it teaches |")
        lines.append("|---|---|---|")
        for s in skills:
            desc = (s.get("description") or "").replace("|", "/")
            lines.append(
                f"| `{s.get('name', '')}` | "
                f"{'yes' if s.get('installed') else 'no — admin install needed'} | {desc} |",
            )
    else:
        lines.append("_None available._")

    taken = data.get("agent_slugs", [])
    if taken:
        lines.append(f"\n# Slugs already taken\n{', '.join(f'`{s}`' for s in taken)}")
    return "\n".join(lines)


def _format_plan(result: dict) -> list[str]:
    """Render the shared validate/create plan blocks."""
    lines: list[str] = []
    mcp_plan = result.get("mcp_plan") or {}
    ready = mcp_plan.get("ready") or []
    needs_install = mcp_plan.get("needs_install") or []
    needs_request = mcp_plan.get("needs_admin_request") or []
    if ready or needs_install or needs_request:
        lines.append("\n**MCPs**")
        if ready:
            lines.append(f"- ready: {', '.join(ready)}")
        if needs_install:
            lines.append(f"- will be installed: {', '.join(needs_install)}")
        if needs_request:
            lines.append(
                f"- needs an admin to approve: {', '.join(needs_request)}",
            )
    skill_plan = result.get("skill_plan") or {}
    if any(skill_plan.values()):
        lines.append("\n**Skill packages**")
        for key, label in (
            ("ready", "ready"),
            ("missing", "not in any catalog"),
            ("admin_required", "needs a platform admin to install"),
        ):
            if skill_plan.get(key):
                lines.append(f"- {label}: {', '.join(skill_plan[key])}")
    seeds = result.get("seeds") or {}
    if seeds:
        bits = [
            f"{seeds.get('tasks', 0)} task(s)",
            f"{seeds.get('triggers', 0)} trigger(s)",
            f"{seeds.get('notifications', 0)} notification(s)",
            f"{seeds.get('context_files', 0)} context file(s)",
        ]
        if seeds.get("has_setup"):
            bits.append("agent setup guide")
        if seeds.get("has_user_setup"):
            bits.append("per-user onboarding")
        lines.append(f"\n**Will seed**: {', '.join(bits)}")
    return lines


async def _tool_validate_agent_template(path: str) -> str:
    if not path.strip():
        return "❌ Error: path is required"
    result = await _request(
        "POST", "/v1/agents/local-template/validate", json={"path": path.strip()},
    )
    if not isinstance(result, dict):
        return "❌ Error: unexpected response from validate"

    if not result.get("ok"):
        errors = result.get("errors") or ["unknown validation error"]
        body = "\n".join(f"- {e}" for e in errors)
        return f"❌ Template is not valid yet:\n{body}\n\nFix these and validate again."

    lines = [f"✅ Template is valid — slug `{result.get('slug', '')}`."]
    if not result.get("slug_available", True):
        lines.append(
            f"⚠️ That slug is taken. Pass `target_slug` to `create_agent` — "
            f"suggested: `{result.get('suggested_slug', '')}`.",
        )
    lines.extend(_format_plan(result))
    lines.append("\nCall `create_agent` with the same path to install it.")
    return "\n".join(lines)


async def _tool_create_agent(path: str, target_slug: str) -> str:
    if not path.strip():
        return "❌ Error: path is required"
    body: dict[str, Any] = {"path": path.strip()}
    if target_slug.strip():
        body["target_slug"] = target_slug.strip()
    result = await _request(
        "POST", "/v1/agents/install-from-local-template", json=body,
    )
    if not isinstance(result, dict):
        return "❌ Error: unexpected response from install"

    slug = result.get("agent_slug", "")
    lines = [
        f"✅ Created agent `{slug}` — you are its manager.",
        f"Open it at {result.get('dashboard_path', f'/agents/{slug}')}",
    ]
    ready = result.get("ready_mcps") or []
    if ready:
        lines.append(f"\n**MCPs enabled**: {', '.join(ready)}")
    requests = result.get("created_requests") or []
    if requests:
        names = ", ".join(
            r.get("mcp_name", "") if isinstance(r, dict) else str(r) for r in requests
        )
        lines.append(
            f"\n⏳ **Waiting on a platform admin** to approve: {names}. "
            "Those tools appear once approved.",
        )
    skills = result.get("skill_packages") or {}
    if skills.get("failed"):
        lines.append(f"\n⚠️ Skill packages that failed: {', '.join(skills['failed'])}")
    seeded = [
        f"{result.get('seeded_tasks', 0)} task(s)",
        f"{result.get('seeded_triggers', 0)} trigger(s)",
        f"{result.get('seeded_notifications', 0)} notification(s)",
    ]
    lines.append(f"\n**Seeded**: {', '.join(seeded)}")
    if result.get("setup_md_copied"):
        lines.append(
            "The agent has a setup guide in its context — open a chat with it "
            "to finish configuration.",
        )
    if result.get("ignored_fields"):
        lines.append(
            f"\nℹ️ Ignored (admin-only): {', '.join(result['ignored_fields'])}",
        )
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Tool schemas
# ---------------------------------------------------------------------------

_TOOL_SCHEMAS: dict[str, dict[str, Any]] = {
    "list_building_blocks": {
        "description": (
            "List the MCPs and skill packages a new agent can be given, plus "
            "the agent slugs already taken. Call this BEFORE writing "
            "`mcps.json` — MCP names are canonical platform names and are not "
            "always guessable."
        ),
        "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
    },
    "validate_agent_template": {
        "description": (
            "Dry-run a template folder you have written: checks the manifest, "
            "persona, required MCPs and skill packages, and slug availability "
            "without creating anything. Returns the exact errors to fix. "
            "Always validate before `create_agent`."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": (
                        "Path to the template folder you wrote, e.g. "
                        "`/workspace/new-agents/research-assistant`."
                    ),
                },
            },
            "required": ["path"],
            "additionalProperties": False,
        },
    },
    "create_agent": {
        "description": (
            "Create a new agent on the platform from a validated template "
            "folder. You become its manager. Required MCPs are installed (or "
            "queued for admin approval), skill packages assigned, and any "
            "scheduled tasks, triggers, notifications and setup guides seeded."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "Path to the template folder.",
                },
                "target_slug": {
                    "type": "string",
                    "description": (
                        "Optional — install under a different slug than the "
                        "manifest's (use when the manifest slug is taken)."
                    ),
                },
            },
            "required": ["path"],
            "additionalProperties": False,
        },
    },
}


_TOOL_HANDLERS = {
    "list_building_blocks": lambda args: _tool_list_building_blocks(),
    "validate_agent_template": lambda args: _tool_validate_agent_template(
        args.get("path", ""),
    ),
    "create_agent": lambda args: _tool_create_agent(
        args.get("path", ""), args.get("target_slug", ""),
    ),
}


# ---------------------------------------------------------------------------
# MCP server
# ---------------------------------------------------------------------------

server = Server("agent-creator-mcp")


@server.list_tools()
async def list_tools() -> list[Tool]:
    return [
        Tool(name=name, description=schema["description"], inputSchema=schema["inputSchema"])
        for name, schema in _TOOL_SCHEMAS.items()
        if name in ENABLED_TOOLS
    ]


@server.call_tool()
async def call_tool(name: str, arguments: dict[str, Any]) -> list[TextContent]:
    if name not in ENABLED_TOOLS:
        return [TextContent(type="text", text=f"❌ Error: tool '{name}' not available in this session")]
    handler = _TOOL_HANDLERS.get(name)
    if handler is None:
        return [TextContent(type="text", text=f"❌ Error: unknown tool '{name}'")]
    try:
        result = await handler(arguments or {})
    except _ApiError as exc:
        return [TextContent(type="text", text=f"❌ Error: {exc}")]
    except Exception as exc:
        return [TextContent(type="text", text=f"❌ Error: {type(exc).__name__}: {exc}")]
    return [TextContent(type="text", text=str(result))]


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

async def main() -> None:
    from mcp.server.stdio import stdio_server
    async with stdio_server() as (read_stream, write_stream):
        await server.run(
            read_stream, write_stream,
            server.create_initialization_options(),
        )


if __name__ == "__main__":
    asyncio.run(main())
