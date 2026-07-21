#!/usr/bin/env python3
"""PostToolUse hook: forwards a brief tool result summary to the proxy
so it can be rendered inline in the chat UI.

Same pattern as permission_gate.py: reads JSON from stdin, uses only
urllib.request (no dependencies), POSTs to the proxy, exits quickly.

Environment variables (set by core/sandbox/env_builder.py in the CLI's subprocess env):
  PROXY_URL         - e.g. http://127.0.0.1:8400
  PROXY_API_KEY     - Bearer token for auth
  OTO_SESSION_ID - session UUID for this conversation
"""

import contextlib
import json
import os
import sys
import urllib.request
import urllib.error

# Tools that already have dedicated rich rendering — skip to avoid noise.
_SKIP_TOOLS = {
    "AskUserQuestion",      # renders formatted question cards
    "Task",                 # renders agent spawn info
    "EnterPlanMode",        # meta-action
    "ExitPlanMode",         # meta-action
    "TaskCreate",           # task management shown via tool_info
    "TaskUpdate",           # task management shown via tool_info
    "TaskList",             # task management shown via tool_info
    "TaskGet",              # task management shown via tool_info
    "TaskStop",             # task management shown via tool_info
    "TaskOutput",           # task management shown via tool_info
}

# MCP tools to skip (display-mcp tools — the image/link itself just appeared)
_SKIP_MCP_TOOLS = {
    "mcp__display__display_image",
    "mcp__display__send_url",
    "mcp__display__send_file",
}


def _extract_result_text(tool_result: dict) -> str:
    """Extract the full text content from a tool result."""
    result_text = ""
    if isinstance(tool_result, dict):
        result_text = (
            tool_result.get("content", "")
            or tool_result.get("text", "")
            or tool_result.get("stdout", "")
            or ""
        )
        if isinstance(result_text, list):
            parts = []
            for block in result_text:
                if isinstance(block, dict) and block.get("type") == "text":
                    parts.append(block.get("text", ""))
            result_text = "\n".join(parts)
        # Read responses nest the body under file.content — without this
        # every Read pill summarized as "empty file" even though the read
        # returned content (live find 2026-07-11).
        if (not isinstance(result_text, str) or not result_text.strip()) \
                and isinstance(tool_result.get("file"), dict):
            file_content = tool_result["file"].get("content")
            if isinstance(file_content, str) and file_content.strip():
                result_text = file_content
        # Bash responses split stdout/stderr — surface both (codex's
        # aggregatedOutput interleaves them; keep the pills comparable).
        stderr = tool_result.get("stderr", "")
        if isinstance(stderr, str) and stderr.strip() and isinstance(result_text, str):
            result_text = f"{result_text.rstrip()}\n{stderr}" if result_text.strip() else stderr
    elif isinstance(tool_result, str):
        result_text = tool_result

    if not isinstance(result_text, str):
        result_text = str(result_text) if result_text else ""
    return result_text


def _extract_summary(tool_name: str, tool_input: dict, result_text: str) -> str:
    """Extract a one-line summary from the tool result text."""

    # Bash: show line count
    if tool_name == "Bash":
        lines = result_text.count("\n") + 1 if result_text.strip() else 0
        return f"{lines} lines" if lines else "ok"

    # Grep: match count
    if tool_name == "Grep":
        if not result_text.strip():
            return "no matches"
        lines = [l for l in result_text.strip().splitlines() if l.strip()]
        return f"{len(lines)} results"

    # Glob: file count
    if tool_name == "Glob":
        if not result_text.strip():
            return "no files"
        lines = [l for l in result_text.strip().splitlines() if l.strip()]
        return f"{len(lines)} files"

    # Read: line count
    if tool_name == "Read":
        if not result_text.strip():
            return "empty file"
        lines = result_text.count("\n") + 1
        return f"{lines} lines"

    # Write/Edit: ok
    if tool_name in ("Write", "Edit"):
        if "error" in result_text.lower()[:100]:
            first_line = result_text.strip().splitlines()[0] if result_text.strip() else ""
            return f"error: {first_line[:80]}"
        return "ok"

    # MCP tools: check for error, otherwise "ok"
    if tool_name.startswith("mcp__"):
        if not result_text.strip():
            return "ok"
        first_line = result_text.strip().splitlines()[0]
        if "error" in first_line.lower()[:100]:
            return f"error: {first_line[:80]}"
        return "ok"

    # Default
    if not result_text.strip():
        return "ok"
    first_line = result_text.strip().splitlines()[0]
    if "error" in first_line.lower()[:100]:
        return f"error: {first_line[:80]}"
    return "ok"


def _is_error_result(tool_result, summary: str) -> bool:
    """Did this tool call fail?

    Reported to the proxy so the MCP cost engine can skip charging for a failed
    call (a failed image generation shouldn't cost credits). Prefer the
    structured MCP error flag; fall back to the summary, which already
    classifies an ``Error…``-prefixed result as an error — some MCPs (e.g.
    image-gen) return failures as plain text rather than setting is_error.
    """
    if isinstance(tool_result, dict) and (
        tool_result.get("is_error") or tool_result.get("isError")
    ):
        return True
    return summary.lower().startswith("error")


def main():
    try:
        inp = json.loads(sys.stdin.read())
    except (json.JSONDecodeError, ValueError):
        return

    session_id = os.environ.get("OTO_SESSION_ID", "")
    proxy_url = os.environ.get("PROXY_URL", "")
    api_key = os.environ.get("PROXY_API_KEY", "")

    if not proxy_url or not api_key or not session_id:
        return

    # Interactive TUI (OTO_INTERACTIVE set by the spawn): rendering is
    # redundant — the terminal shows tool results itself, and forwarding was
    # surfacing "PostToolUse hook error" noise. But a successful mcp__ tool
    # call still needs to reach the proxy's session allow-memory: execution
    # is the only evidence the user clicked Allow in the native prompt, and
    # without it every call of the same tool re-prompts. Send a MEMORY-ONLY
    # ping for mcp__ tools (the proxy skips rendering for it); no-op for
    # everything else. Headless -p still forwards everything.
    if os.environ.get("OTO_INTERACTIVE"):
        tool_name = inp.get("tool_name", "")
        if not tool_name.startswith("mcp__"):
            return
        tool_result = inp.get("tool_response") or inp.get("tool_result") or {}
        payload = json.dumps({
            "session_id": session_id,
            "tool_name": tool_name,
            "summary": "",
            "is_error": _is_error_result(tool_result, ""),
            "memory_only": True,
        }).encode()
        req = urllib.request.Request(
            f"{proxy_url}/v1/hooks/tool-result",
            data=payload,
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
        )
        # memory feed is best-effort — never surface hook errors
        with contextlib.suppress(Exception):
            urllib.request.urlopen(req, timeout=10)
        return

    tool_name = inp.get("tool_name", "")
    tool_input = inp.get("tool_input", {})
    # The CLI's PostToolUse input carries the result under ``tool_response``
    # (verified live, CLI 2.1.201); the old ``tool_result`` read matched
    # nothing, so every headless Claude pill shipped an EMPTY body ("ok"
    # summaries, no Output section). Keep the legacy key as a fallback.
    tool_result = inp.get("tool_response") or inp.get("tool_result") or {}

    # Skip tools with dedicated rendering
    if tool_name in _SKIP_TOOLS or tool_name in _SKIP_MCP_TOOLS:
        return

    result_text = _extract_result_text(tool_result)
    summary = _extract_summary(tool_name, tool_input, result_text)
    if not summary:
        return

    # Cap result content to avoid huge payloads (500 lines or 50KB)
    result_content = result_text
    if result_content:
        lines = result_content.split("\n")
        if len(lines) > 500:
            result_content = "\n".join(lines[:500]) + f"\n... ({len(lines) - 500} more lines)"
        if len(result_content) > 50000:
            result_content = result_content[:50000] + "\n... (truncated)"

    payload = json.dumps({
        "session_id": session_id,
        "tool_name": tool_name,
        # Exact correlation key (parallel same-name tools; Agent results
        # attach to their task_spawn block by this id).
        "tool_use_id": inp.get("tool_use_id", "") or "",
        "summary": summary,
        "result_content": result_content,
        "is_error": _is_error_result(tool_result, summary),
    }).encode()

    req = urllib.request.Request(
        f"{proxy_url}/v1/hooks/tool-result",
        data=payload,
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
    )

    try:
        with urllib.request.urlopen(req, timeout=5):
            pass  # Fire and forget
    except Exception:
        pass  # Non-blocking — don't interrupt Claude


if __name__ == "__main__":
    main()
