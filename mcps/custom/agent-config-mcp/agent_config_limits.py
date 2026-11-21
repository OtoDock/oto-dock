"""The agent's tool limits, as its self-config tools read and change them
(``GET`` / ``PUT /v1/agents/{slug}/tool-limits``): which functions of each
enabled MCP the agent may use in its own runs and in people's chats. A
session may only tighten a limit (add one, or shrink a custom list): the
platform refuses the rest and the tool answers its sentence. The bodies live
here, beside ``server.py``, which wires them into its handler table."""

from __future__ import annotations

from typing import Any

#: The two columns a limit is set for, in the card's order, and their words.
COLUMNS = ("runs", "chats")
COLUMN_WORDS = {"outside": "External identities", "runs": "The agent's own runs", "chats": "People's chats"}


def limits_lines(agent_name: str, page: dict) -> list[str]:
    """The page as the card shows it, one line per tool and column."""
    rows = [f"# What `{agent_name}` may use",
            "Which tools of each MCP this agent may use, by who runs the session. All tools unless limited. "
            "A limit holds on every engine and in every mode; a tool left off is refused when called."]
    tools = page.get("tools") or []
    if not tools:
        rows.append("- This agent has no tools to limit.")
    for t in tools:
        known = t.get("known_functions")
        listed = (", ".join(f["name"] + (" (reads)" if f.get("reads_only") else "") for f in known)
                  if known else "not listed yet")
        rows.append(f"- **{t.get('mcp')}** ({t.get('label')}): functions {listed}")
        for column in ("outside", "runs", "chats"):
            rows.append(f"  - {COLUMN_WORDS[column]}: {_state(t.get(column), column)}")
    return rows


def _state(limit: dict | None, column: str) -> str:
    if limit is None:
        return "not added" if column == "outside" else "all tools"
    if limit.get("access") == "custom":
        return "only " + ", ".join(limit.get("functions") or [])
    if limit.get("access") == "read_write":
        return "read and write"
    changed = " (the maker changed which functions only read since this was saved)" if limit.get("changed") else ""
    return "only the tools that read" + changed


def limits_body(args: dict) -> tuple[dict | None, str]:
    """The route's body for a ``set_tool_limits`` call, or an error line.
    ``tools``: a list of ``{mcp, runs?, chats?}``, each column ``read_only``
    or a list of function names; ``remove``: a list of ``{mcp, column}``."""
    body: dict[str, Any] = {}
    if "tools" in args:
        tools = args["tools"]
        if not isinstance(tools, list):
            return None, "❌ Error: `tools` is a list of {mcp, runs?, chats?}"
        changes = []
        for t in tools:
            if not isinstance(t, dict) or not t.get("mcp"):
                return None, "❌ Error: each tool is {mcp, runs?, chats?}"
            change: dict[str, Any] = {"mcp": t["mcp"]}
            for column in COLUMNS:
                if column not in t or t[column] is None:
                    continue
                value = t[column]
                if value == "read_only":
                    change[column] = {"access": "read_only", "functions": []}
                elif isinstance(value, list):
                    change[column] = {"access": "custom", "functions": [str(f) for f in value]}
                else:
                    return None, f"❌ Error: `{column}` is read_only or a list of function names"
            if len(change) == 1:
                return None, f"❌ Error: name a column for {t['mcp']}: runs or chats"
            changes.append(change)
        body["tools"] = changes
    if "remove" in args:
        remove = args["remove"]
        if not isinstance(remove, list):
            return None, "❌ Error: `remove` is a list of {mcp, column}"
        removals = []
        for r in remove:
            if not isinstance(r, dict) or not r.get("mcp") or r.get("column") not in COLUMNS:
                return None, "❌ Error: each removal is {mcp, column}, the column runs or chats"
            removals.append({"mcp": r["mcp"], "governs": r["column"]})
        body["remove"] = removals
    if not body:
        return None, "❌ Error: name at least one thing to change"
    return body, ""
