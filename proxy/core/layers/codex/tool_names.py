"""Codex's native tool names → the platform's canonical names
(``core/events/tool_roles``).

One engine, four native spellings of the same calls: the app-server's
ThreadItem types (``commandExecution`` / ``fileChange`` / ``webSearch``), the
rollout's function-call names (``exec_command`` / ``update_plan`` /
``request_user_input``), its custom tools (``apply_patch``, the code-mode
``exec``) and its server-side call types (``web_search_call`` /
``tool_search_call``). Every one maps here, once; the translator, the
rollout tailer and the layer's ``canonical_tool_name`` read this table. The
canonical side is frozen — the names persisted in ``chat_messages`` and
keyed on by the permission authority. A stdlib-only leaf of the package.
"""

from __future__ import annotations

NATIVE_TO_CANONICAL: dict[str, str] = {
    "commandExecution": "Bash",           # app-server item type
    "exec_command": "Bash",               # rollout function call
    "exec": "Bash",                       # rollout custom tool (code mode; the tailer keeps its parse guard)
    "fileChange": "apply_patch",          # app-server item type
    "apply_patch": "apply_patch",         # rollout custom tool
    "webSearch": "web_search",            # app-server item type
    "web_search_call": "web_search",      # rollout server-side call
    "tool_search_call": "ToolSearch",     # rollout server-side call
    "update_plan": "TodoWrite",           # rollout function call (the checklist snapshot)
    "request_user_input": "request_user_input",
    "CodexEscalation": "CodexEscalation", # the approval bridge's synthetic tool
    "CodexTerminalInput": "CodexTerminalInput",  # the bridge's terminal-input approval (0.158+)
}

#: The app-server item types that are tool calls (the translator emits
#: TOOL_USE / TOOL_INPUT / TOOL_RESULT for exactly these).
TOOL_ITEM_TYPES: tuple[str, ...] = ("commandExecution", "fileChange", "webSearch")


def canonical(native: str) -> str:
    """The canonical name for a Codex-native one; unknown names pass through
    (an MCP tool, a name this table has not met)."""
    return NATIVE_TO_CANONICAL.get(native or "", native or "")


def sanitized_server_name(name: str) -> str:
    """The form Codex gives an MCP server key in its hook tool names
    (``meetings-mcp`` → ``meetings_mcp``)."""
    return "".join(c if c.isalnum() or c == "_" else "_" for c in name)
