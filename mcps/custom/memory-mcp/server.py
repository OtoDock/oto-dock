"""Memory MCP Server.

stdio MCP server exposing ONE tool, ``memory``, that mirrors Anthropic's
``memory_20250818`` command contract (view / create / str_replace / insert /
delete / rename) — models are trained on these exact command names, return
strings, and error strings. Every call relays to the proxy
(``POST /v1/internal/memory/op``), which owns ALL semantics: role matrix,
toggles, locking, caps, git attribution, index regeneration, satellite
fan-out. This process is a thin, stateless relay.

Paths are virtual and scope-prefixed:

    /memories/agent/...  → shared with every user of this agent
    /memories/user/...   → private to the current user

The agent's memory CONTENT is injected into its system prompt by the proxy
(full topic files while small, the generated MEMORY.md index past the
budget) — so the tool exists to WRITE/UPDATE/DELETE, plus ``view`` for
topics that weren't inlined.

Sessions where both memory scopes are disabled get an empty tool list.

Environment variables (set in per-agent mcp-config.json via manifest
``agent_env`` + proxy env injection):

  MEMORY_MCP_AGENT     — Which agent this instance serves
  PROXY_URL            — Proxy URL (e.g. http://localhost:8400)
  PROXY_API_KEY        — Session-scoped JWT (auto-injected by proxy)
  PROXY_TASK_OWNER     — user_sub for task sessions (X-On-Behalf-Of)
  PROXY_TASK_SCOPE     — "user" | "agent" — task scope (fallback)
  OTO_USER_SUB         — user_sub for chat sessions (fallback)
  OTO_DEFAULT_SCOPE    — per-agent default scope
  OTO_SCOPE            — actual session scope (fallback)
  OTO_MEMORY_USER_ENABLED  — "false" hides the user scope
  OTO_MEMORY_AGENT_ENABLED — "false" hides the agent scope
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import re
from typing import Any

import httpx
from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import TextContent, Tool


AGENT = os.environ.get("MEMORY_MCP_AGENT", "")
PROXY_URL = os.environ.get("PROXY_URL", "http://localhost:8400").rstrip("/")
API_KEY = (
    os.environ.get("MEMORY_MCP_API_KEY")
    or os.environ.get("PROXY_API_KEY", "")
)

# Identity for X-On-Behalf-Of. Chat sessions use OTO_USER_SUB; task sessions
# use PROXY_TASK_OWNER (set by manifest ``agent_env`` from ``${session.task_owner}``).
USER_SUB = (
    os.environ.get("PROXY_TASK_OWNER")
    or os.environ.get("OTO_USER_SUB")
    or ""
)

# OTO_DEFAULT_SCOPE drives the default scope across every scope-aware MCP.
# PROXY_TASK_SCOPE / OTO_SCOPE are fallbacks (the actual session scope).
DEFAULT_SCOPE = (
    os.environ.get("OTO_DEFAULT_SCOPE")
    or os.environ.get("PROXY_TASK_SCOPE")
    or os.environ.get("OTO_SCOPE")
    or ("user" if USER_SUB else "agent")
)

# Feature toggles (resolved per-session at env-build time by the proxy).
# Mid-session toggle flips don't propagate — the env is frozen at start;
# the API re-checks server-side as belt-and-braces.
USER_MEMORY_ENABLED = (
    os.environ.get("OTO_MEMORY_USER_ENABLED", "true").lower() == "true"
)
AGENT_MEMORY_ENABLED = (
    os.environ.get("OTO_MEMORY_AGENT_ENABLED", "true").lower() == "true"
)


server = Server("memory-mcp")


def _headers() -> dict[str, str]:
    h = {
        "Authorization": f"Bearer {API_KEY}",
        "X-Agent-Name": AGENT,
        "Content-Type": "application/json",
    }
    if USER_SUB:
        h["X-On-Behalf-Of"] = USER_SUB
    return h


async def _post_op(body: dict[str, Any]) -> dict[str, Any]:
    async with httpx.AsyncClient(timeout=30.0) as client:
        resp = await client.post(
            f"{PROXY_URL}/v1/internal/memory/op",
            json=body, headers=_headers(),
        )
        resp.raise_for_status()
        return resp.json()


# ---------------------------------------------------------------------------
# Tool definition
# ---------------------------------------------------------------------------

def _available_scopes() -> list[str]:
    scopes: list[str] = []
    if AGENT_MEMORY_ENABLED:
        scopes.append("agent")
    if USER_SUB and USER_MEMORY_ENABLED:
        scopes.append("user")
    return scopes


def _default_scope() -> str:
    """The scope the tool description advertises as the default. Bare-filename
    ``view`` inference must agree with it — the two resolving differently
    would read one scope while advertising another."""
    scopes = _available_scopes()
    return DEFAULT_SCOPE if DEFAULT_SCOPE in scopes else (scopes[0] if scopes else "agent")


def _memory_tool() -> Tool:
    scopes = _available_scopes()
    default = _default_scope()
    scope_lines = []
    if "agent" in scopes:
        scope_lines.append(
            "/memories/agent/ — shared with every user of this agent "
            "(operational facts, conventions, workflows, shared project state)"
        )
    if "user" in scopes:
        scope_lines.append(
            "/memories/user/ — private to the current user "
            "(their preferences, context, facts about them)"
        )
    description = (
        "Manage your persistent memory — markdown topic files that survive "
        "across sessions. Available scopes:\n"
        + "\n".join(f"  {s}" for s in scope_lines)
        + f"\nDefault scope: /memories/{default}/. "
        "Your memory content is already shown in your system prompt (full "
        "topics while small, the index past that) — use this tool to SAVE, "
        "UPDATE, or DELETE memories the moment you learn something durable, "
        "and `view` to read topics that weren't inlined. Update existing "
        "topics instead of creating duplicates; date entries (YYYY-MM-DD); "
        "start each topic file with a one-line `# heading` (it becomes the "
        "index entry). Never store secrets/credentials. MEMORY.md is "
        "auto-generated — edit topic files only.\n"
        "Commands: view (directory listing / numbered file read), create "
        "(new topic file — errors if it exists), str_replace (replace a "
        "unique string), insert (insert text at a line), delete, rename."
    )
    return Tool(
        name="memory",
        description=description,
        inputSchema={
            "type": "object",
            "properties": {
                "command": {
                    "type": "string",
                    "enum": [
                        "view", "create", "str_replace",
                        "insert", "delete", "rename",
                    ],
                    "description": "The memory operation to perform.",
                },
                "path": {
                    "type": "string",
                    "description": (
                        "Target path (all commands except rename), e.g. "
                        "/memories/user/preferences.md"
                    ),
                },
                "view_range": {
                    "anyOf": [
                        {"type": "array", "items": {"type": "integer"}},
                        {"type": "string"},
                    ],
                    "description": (
                        "view only: [start_line, end_line] (1-indexed, "
                        "end -1 = EOF)."
                    ),
                },
                "file_text": {
                    "type": "string",
                    "description": "create only: the full file content.",
                },
                "old_str": {
                    "type": "string",
                    "description": (
                        "str_replace only: exact text to replace (must "
                        "appear exactly once)."
                    ),
                },
                "new_str": {
                    "type": "string",
                    "description": "str_replace only: replacement text.",
                },
                "insert_line": {
                    "anyOf": [
                        {"type": "integer"},
                        {"type": "string"},
                    ],
                    "description": (
                        "insert only: line number to insert AFTER "
                        "(0 = top of file)."
                    ),
                },
                "insert_text": {
                    "type": "string",
                    "description": "insert only: the text to insert.",
                },
                "old_path": {
                    "type": "string",
                    "description": "rename only: current path.",
                },
                "new_path": {
                    "type": "string",
                    "description": "rename only: new path (must not exist).",
                },
            },
            "required": ["command"],
        },
    )


@server.list_tools()
async def list_tools() -> list[Tool]:
    # No memory scopes enabled for this session → no tool.
    if not _available_scopes():
        return []
    return [_memory_tool()]


# ---------------------------------------------------------------------------
# Tolerant argument coercion
# ---------------------------------------------------------------------------
#
# CLI tool-search deferral can make agents call tools "blind" (before the
# declared schema is loaded), passing JSON-ish strings for typed args — the
# same fragility the meetings tools hit. Coerce instead of bouncing.

class ToolArgError(Exception):
    """Self-correcting argument error — returned as the tool-result text."""


_CANONICAL_COMMANDS = {"view", "create", "str_replace", "insert", "delete", "rename"}
# Near-miss command names map onto the contract by TOKEN, never substring —
# substring matching would send `reformat` to delete (contains "rm") and
# `truncate`/`locate` to view (contains "cat"). The destructive buckets
# (delete/rename) match only these exact tokens. Order matters: str_replace
# outranks create so editor-flavoured names never create files, and the
# non-destructive buckets outrank the destructive ones so an ambiguous name
# can never delete.
_COMMAND_TOKEN_MAP: tuple[tuple[str, frozenset[str]], ...] = (
    ("str_replace", frozenset({"replace", "edit"})),
    ("create", frozenset({"create", "write"})),
    ("view", frozenset({"view", "read", "cat", "list", "ls", "show"})),
    ("insert", frozenset({"insert", "append"})),
    ("delete", frozenset({"delete", "del", "rm", "remove", "unlink"})),
    ("rename", frozenset({"rename", "move", "mv"})),
)
_WRITE_PAYLOAD_KEYS = (
    "file_text", "content", "old_str", "old_string",
    "new_str", "new_string", "insert_text",
)


def _normalize_command(arguments: dict[str, Any]) -> str | None:
    """Map a near-miss command onto its canonical contract name.

    ``str_replace_based_edit_20250124`` → str_replace, ``remove_topic`` →
    delete. A missing command with a path and no write payload infers
    ``view``; anything unrecognised passes through so the proxy's
    self-correcting error (which lists the valid commands) reaches the model.
    """
    command = arguments.get("command")
    if not command or not isinstance(command, str):
        if arguments.get("path") and not any(
            arguments.get(k) for k in _WRITE_PAYLOAD_KEYS
        ):
            return "view"
        return command if isinstance(command, str) and command else None
    if command in _CANONICAL_COMMANDS:
        return command
    tokens = set(re.split(r"[^a-z]+", command.lower())) - {""}
    for canonical, wanted in _COMMAND_TOKEN_MAP:
        if tokens & wanted:
            return canonical
    return command


def _normalize_path(command: str | None, path: Any) -> Any:
    """Root sentinels + bare-filename policy.

    ``view``: missing/``""``/``"/"`` list the /memories root, and a bare
    filename reads from the advertised default scope (a wrong guess is a
    harmless "does not exist"). Writes never scope-guess: the fixed scope
    order would land a bare-filename personal note in the SHARED agent
    scope, so they are refused with an error naming both scopes instead.
    """
    p = path.strip() if isinstance(path, str) else path
    bare = isinstance(p, str) and p and "/" not in p and p != "memories"
    if command == "view":
        if p is None or p == "" or p == "/":
            return "/memories"
        if bare:
            return f"/memories/{_default_scope()}/{p}"
        return p
    if bare:
        raise ToolArgError(
            f"Ambiguous path {p!r}: specify the scope — /memories/agent/{p} "
            f"(shared with every user of this agent) or /memories/user/{p} "
            "(private to the current user)."
        )
    return p


def _coerce_view_range(v: Any) -> list[int] | None:
    if v is None:
        return None
    if isinstance(v, str):
        try:
            v = json.loads(v)
        except (ValueError, TypeError):
            return None
    if isinstance(v, (list, tuple)):
        try:
            return [int(x) for x in v]
        except (ValueError, TypeError):
            return None
    return None


def _coerce_int(v: Any) -> int | None:
    if isinstance(v, bool):
        return None
    if isinstance(v, int):
        return v
    if isinstance(v, str):
        try:
            return int(v.strip())
        except ValueError:
            return None
    return None


def build_op_body(name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    """Map tool arguments onto the /op request body (pure, testable).

    Normalizes the command FIRST so a missing command with a path can still
    infer ``view``; raises ToolArgError for the bare-filename write refusal.
    """
    command = _normalize_command(arguments)
    body: dict[str, Any] = {"command": command}
    if command == "rename":
        body["old_path"] = _normalize_path(
            command, arguments.get("old_path") or arguments.get("path"))
        body["new_path"] = _normalize_path(command, arguments.get("new_path"))
    else:
        body["path"] = _normalize_path(command, arguments.get("path"))
    if command == "view":
        vr = _coerce_view_range(arguments.get("view_range"))
        if vr is not None:
            body["view_range"] = vr
    elif command == "create":
        # `content` = the built-in Write tool's parameter name — models reach
        # for it out of habit; accept it as an alias so the call succeeds
        # first try. Same for the Edit-tool names on str_replace below.
        body["file_text"] = (
            arguments.get("file_text") or arguments.get("content") or ""
        )
    elif command == "str_replace":
        body["old_str"] = (
            arguments.get("old_str") or arguments.get("old_string") or ""
        )
        body["new_str"] = (
            arguments.get("new_str") or arguments.get("new_string") or ""
        )
    elif command == "insert":
        line = _coerce_int(arguments.get("insert_line"))
        if line is not None:
            body["insert_line"] = line
        body["insert_text"] = arguments.get("insert_text") or ""
    return body


def format_result(data: dict[str, Any]) -> str:
    """The /op response → tool-result text. ``output`` is relayed VERBATIM
    (models are trained on the contract strings); warnings append below."""
    out = data.get("output", "")
    for w in data.get("warnings") or []:
        out += f"\n{w}"
    return out


# ---------------------------------------------------------------------------
# Tool dispatch
# ---------------------------------------------------------------------------

@server.call_tool(validate_input=False)
async def call_tool(name: str, arguments: Any) -> list[TextContent]:
    try:
        # validate_input=False also drops the SDK's dict guarantee — a blind
        # caller can pass a JSON string (or junk); coerce rather than crash.
        if isinstance(arguments, str):
            try:
                arguments = json.loads(arguments)
            except (ValueError, TypeError):
                arguments = {}
        if not isinstance(arguments, dict):
            arguments = {}
        if name != "memory":
            return [TextContent(type="text", text=f"Unknown tool: {name}")]
        if not _available_scopes():
            return [TextContent(
                type="text",
                text="memory is not enabled for this session.",
            )]
        body = build_op_body(name, arguments)
        if not body.get("command"):
            return [TextContent(type="text", text="command required")]
        data = await _post_op(body)
        return [TextContent(type="text", text=format_result(data))]

    except ToolArgError as e:
        return [TextContent(type="text", text=str(e))]
    except httpx.HTTPStatusError as e:
        # Unwrap FastAPI's {"detail": ...} so the model reads the actual
        # message, not a JSON blob.
        detail = None
        with contextlib.suppress(Exception):
            detail = e.response.json().get("detail")
        text = detail if isinstance(detail, str) else e.response.text
        return [TextContent(
            type="text",
            text=f"API error {e.response.status_code}: {text}",
        )]
    except Exception as e:
        return [TextContent(type="text", text=f"Error: {e}")]


async def main() -> None:
    async with stdio_server() as (read_stream, write_stream):
        await server.run(
            read_stream, write_stream,
            server.create_initialization_options(),
        )


if __name__ == "__main__":
    asyncio.run(main())
