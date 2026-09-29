"""Checks MCP — named checks that judge an agent's work (proxy
docs/features/CHECKS.md).

Every tool is a proxy route; the session JWT (``PROXY_API_KEY``) is the
authority — the routes resolve the calling session's chat from it and gate
the managers' tools on the per-agent role. The env gate here
(``OTO_CAN_MANAGE_AGENT``, ``OTO_TASK_TYPE``) only hides tools that cannot
succeed:

- everyone: ``list_checks``, ``attach_check``, ``detach_check`` (not in a
  task's or a delegation's run), ``run_check``; on modes with personal sessions ``create_private_check``
  and ``delete_private_check`` (the caller's own tree);
- managers and admins, with a human present: ``set_check`` and
  ``delete_check`` (the agent's checks in its config).
"""

import asyncio
import contextlib
import json
import os
from typing import Any

import httpx
from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import TextContent, Tool

AGENT = os.environ.get("CHECKS_MCP_AGENT", "") or os.environ.get("OTO_AGENT_NAME", "")
# The owner-tier question, answered by the proxy in the env
# (core/sandbox/oto_env.py): a separate process cannot import the proxy and
# carries no role vocabulary of its own.
CAN_MANAGE = os.environ.get("OTO_CAN_MANAGE_AGENT", "") == "true"
TASK_TYPE = os.environ.get("OTO_TASK_TYPE", "")
USERNAME = os.environ.get("OTO_USERNAME", "")
PROXY_URL = os.environ.get("PROXY_URL", "http://localhost:8400").rstrip("/")
API_KEY = os.environ.get("PROXY_API_KEY", "")

server = Server("checks-mcp")

_EVERYONE = {"list_checks", "attach_check", "detach_check", "run_check"}
_PRIVATE = {"create_private_check", "delete_private_check"}
_MANAGER = {"set_check", "delete_check"}


def _resolve_tool_set() -> set[str]:
    tools = set(_EVERYONE)
    if TASK_TYPE:
        # A run keeps the checks its task or delegation named (the route
        # refuses the detach too).
        tools.discard("detach_check")
    if USERNAME:
        tools |= _PRIVATE
    if CAN_MANAGE and not TASK_TYPE:
        tools |= _MANAGER
    return tools


ENABLED_TOOLS = _resolve_tool_set()


def _headers() -> dict[str, str]:
    return {"Authorization": f"Bearer {API_KEY}", "X-Agent-Name": AGENT,
            "Content-Type": "application/json"}


async def _request(method: str, path: str, **kwargs) -> Any:
    async with httpx.AsyncClient(timeout=1900.0) as client:
        resp = await client.request(method, f"{PROXY_URL}{path}", headers=_headers(), **kwargs)
        if resp.status_code >= 400:
            try:
                detail = resp.json().get("detail")
            except Exception:  # noqa: BLE001
                detail = resp.text[:300]
            raise RuntimeError(str(detail or f"HTTP {resp.status_code}"))
        return resp.json() if resp.content else {}


def _text(s: str) -> list[TextContent]:
    return [TextContent(type="text", text=s)]


def _describe(c: dict) -> str:
    kinds = ", ".join(c.get("sections") or [])
    when = "mandatory" if c.get("mandatory") else "offered"
    applies = ", ".join(c.get("applies") or [])
    cond = c.get("condition") or {}
    cond_words = "every turn" if cond.get("always") else (
        ", ".join(f"{k}={v}" for k, v in cond.items()) or "any write or event")
    line = f"- {c['ref']} ({kinds}; {when}; applies to {applies}; runs on {cond_words}; rounds {c.get('rounds')})"
    if c.get("description"):
        line += f"\n    {c['description']}"
    if c.get("problems"):
        line += "\n    ⚠ " + "; ".join(c["problems"])
    return line


async def _tool_list_checks() -> str:
    data = await _request("GET", f"/v1/agents/{AGENT}/checks")
    attached: dict = {}
    with contextlib.suppress(RuntimeError):
        attached = await _request("GET", "/v1/checks/attached")
    lines = [f"Checks on {AGENT}:"]
    items = data.get("checks") or []
    if not items:
        lines.append("  (none yet)")
    lines += [_describe(c) for c in items]
    if attached:
        lines.append("")
        lines.append("Attached to this chat: " + (", ".join(attached.get("attached") or []) or "none")
                     + "; mandatory here: " + (", ".join(attached.get("mandatory") or []) or "none"))
    return "\n".join(lines)


async def _tool_attach_check(name: str) -> str:
    out = await _request("POST", "/v1/checks/attach", json={"ref": name})
    return f"Attached {out['ref']} to this chat. Attached now: {', '.join(out['attached'])}."


async def _tool_detach_check(name: str) -> str:
    out = await _request("POST", "/v1/checks/detach", json={"ref": name})
    return f"Detached {out['ref']}. Attached now: {', '.join(out['attached']) or 'none'}."


async def _tool_run_check(name: str) -> str:
    out = await _request("POST", "/v1/checks/run", json={"ref": name})
    head = f"{out['check']}: {out['status']}" + (f" (score {out['score']})" if out.get("score") is not None else "")
    body = out.get("text") or out.get("summary") or ""
    return f"{head}\n{body}".strip()


def _doc_from(args: dict) -> dict:
    doc = args.get("doc")
    if isinstance(doc, str):
        doc = json.loads(doc)
    if not isinstance(doc, dict):
        raise RuntimeError("doc must be the check document (an object)")
    doc = dict(doc)
    doc["name"] = args["name"]
    return doc


async def _tool_set_check(name: str, doc: Any, script: str | None = None) -> str:
    out = await _request("PUT", f"/v1/agents/{AGENT}/checks/{name}",
                         json={"doc": _doc_from({"name": name, "doc": doc}), "script": script})
    return f"Saved the agent's check {out['ref']} ({', '.join(out['sections'])}; " \
           f"{'mandatory' if out.get('mandatory') else 'offered'}). New sessions see it at their next turn end."


async def _tool_delete_check(name: str) -> str:
    await _request("DELETE", f"/v1/agents/{AGENT}/checks/{name}")
    return f"Removed the agent's check {name}."


async def _tool_create_private_check(name: str, doc: Any, script: str | None = None) -> str:
    out = await _request("PUT", f"/v1/agents/{AGENT}/user-checks/{name}",
                         json={"doc": _doc_from({"name": name, "doc": doc}), "script": script})
    return f"Saved your private check {out['ref']} ({', '.join(out['sections'])}). " \
           f"Attach it with attach_check('{out['ref']}')."


async def _tool_delete_private_check(name: str) -> str:
    await _request("DELETE", f"/v1/agents/{AGENT}/user-checks/{name}")
    return f"Removed your private check {name}."


_DOC_SCHEMA = {
    "type": "object",
    "description": (
        "The check document (the `checks` skill has the full shape): description, "
        "mandatory (managers only), applies [chats|tasks|delegations], condition "
        "{always|kinds|events|places|globs|commands}, rounds 0-3, inputs [tree paths], and at "
        "least one of schema (a JSON schema for the answer), script {run, timeout}, handler "
        "{app, handler}, judge {rubric, engine, model, threshold, mcps, judge_on, timeout}."
    ),
}

_TOOL_SCHEMAS: dict[str, dict] = {
    "list_checks": {
        "description": (
            "List this agent's checks (mandatory and offered), your own private ones, and what is "
            "attached to this chat. A check judges work at the end of a turn and hands the "
            "agent its findings for another round when it fails."
        ),
        "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
    },
    "attach_check": {
        "description": "Attach an offered check (or one of your own) to this chat: it runs at the end of every turn its condition matches.",
        "inputSchema": {"type": "object", "properties": {
            "name": {"type": "string", "description": "The check's name, or its ref (agent:<name> | user:<name>)"}},
            "required": ["name"], "additionalProperties": False},
    },
    "detach_check": {
        "description": "Detach a check from this chat. A mandatory check cannot be detached here — a manager removes it in the agent's Checks page.",
        "inputSchema": {"type": "object", "properties": {
            "name": {"type": "string", "description": "The check's name or ref"}},
            "required": ["name"], "additionalProperties": False},
    },
    "run_check": {
        "description": (
            "Run a check now on this chat's last turn, whatever its condition, and get the verdict "
            "(a judge may take a minute or more). Use it to try a check before attaching it."
        ),
        "inputSchema": {"type": "object", "properties": {
            "name": {"type": "string", "description": "The check's name or ref"}},
            "required": ["name"], "additionalProperties": False},
    },
    "create_private_check": {
        "description": (
            "Create or update one of YOUR checks (in your own tree; it applies to your own "
            "sessions when attached). Give the document and, for the script kind, the script text."
        ),
        "inputSchema": {"type": "object", "properties": {
            "name": {"type": "string", "description": "lowercase letters, digits, - and _"},
            "doc": _DOC_SCHEMA,
            "script": {"type": "string", "description": "The script's text for a script section (a #! line names the interpreter)"}},
            "required": ["name", "doc"], "additionalProperties": False},
    },
    "delete_private_check": {
        "description": "Remove one of your checks.",
        "inputSchema": {"type": "object", "properties": {"name": {"type": "string"}},
                        "required": ["name"], "additionalProperties": False},
    },
    "set_check": {
        "description": (
            "Managers: create or update one of the AGENT's checks (its config, every mode). "
            "mandatory=true makes it run on every session per `applies`; otherwise it is offered "
            "for people to attach. Give the document and, for the script kind, the script text."
        ),
        "inputSchema": {"type": "object", "properties": {
            "name": {"type": "string", "description": "lowercase letters, digits, - and _"},
            "doc": _DOC_SCHEMA,
            "script": {"type": "string", "description": "The script's text for a script section"}},
            "required": ["name", "doc"], "additionalProperties": False},
    },
    "delete_check": {
        "description": "Managers: remove one of the agent's checks (a mandatory one included).",
        "inputSchema": {"type": "object", "properties": {"name": {"type": "string"}},
                        "required": ["name"], "additionalProperties": False},
    },
}

_TOOL_HANDLERS = {
    "list_checks": lambda a: _tool_list_checks(),
    "attach_check": lambda a: _tool_attach_check(a["name"]),
    "detach_check": lambda a: _tool_detach_check(a["name"]),
    "run_check": lambda a: _tool_run_check(a["name"]),
    "create_private_check": lambda a: _tool_create_private_check(a["name"], a.get("doc"), a.get("script")),
    "delete_private_check": lambda a: _tool_delete_private_check(a["name"]),
    "set_check": lambda a: _tool_set_check(a["name"], a.get("doc"), a.get("script")),
    "delete_check": lambda a: _tool_delete_check(a["name"]),
}


@server.list_tools()
async def list_tools() -> list[Tool]:
    return [Tool(name=name, description=spec["description"], inputSchema=spec["inputSchema"])
            for name, spec in _TOOL_SCHEMAS.items() if name in ENABLED_TOOLS]


@server.call_tool()
async def call_tool(name: str, arguments: dict) -> list[TextContent]:
    if name not in ENABLED_TOOLS or name not in _TOOL_HANDLERS:
        return _text(f"❌ {name} is not available in this session.")
    try:
        return _text(await _TOOL_HANDLERS[name](arguments or {}))
    except RuntimeError as e:
        return _text(f"❌ {e}")
    except httpx.HTTPError as e:
        return _text(f"❌ The platform did not answer: {e.__class__.__name__}")


async def main() -> None:
    async with stdio_server() as (read_stream, write_stream):
        await server.run(read_stream, write_stream, server.create_initialization_options())


if __name__ == "__main__":
    asyncio.run(main())
