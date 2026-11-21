"""The permission authority's reading of a tool limit (``services/mcp/tool_limits``):
one branch of ``decide_tool_permission``, before the path gate and every mode
branch, for every session kind and engine. The limit rides the session's
security context, resolved at its build; a tool the limit leaves out is
refused whether or not the engine was told to hide it."""

from __future__ import annotations

from services.mcp import tool_limits


def limit_refusal(ctx, tool_name: str) -> str:
    """The refusal of a call to a function the session's limits leave out,
    "" when the call may go on. An MCP tool's name is ``mcp__<server>__<fn>``
    as the engine's layer canonicalised it; a Codex name the mapping did not
    meet keeps its sanitised server key, so a pair is also matched by that
    spelling: the reading fails closed, never open."""
    if not tool_name.startswith("mcp__"):
        return ""
    parts = tool_name.split("__", 2)
    server, function = (parts[1] if len(parts) >= 2 else ""), (parts[2] if len(parts) >= 3 else "")
    pairs = getattr(ctx, "tool_limits", ()) or ()
    if not pairs or not server:
        return ""
    if tool_limits.limited(pairs, server) is None:
        from core.layers.codex import tool_names
        for key, names in pairs:
            if tool_names.sanitized_server_name(key) == server:
                pairs = ((server, names),)
                break
    return tool_limits.refusal(pairs, server, function)
