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
import re
import sys
import threading
import time
import urllib.request
import urllib.error

# The platform's tool vocabulary by ROLE — a byte twin of
# ``core/events/tool_roles.py:TOOL_ROLES`` (this script runs inside the CLI's
# sandbox and cannot import the proxy; the release gate's twin rule keeps the
# two identical: same key order, same string values). Everything below keys
# on a tool's role, never on its name.
TOOL_ROLES = {
    "Bash": "shell",
    "Monitor": "shell",
    "PowerShell": "shell",
    "Read": "read",
    "Glob": "glob",
    "Grep": "search",
    "Write": "write",
    "Edit": "write",
    "MultiEdit": "write",
    "NotebookEdit": "write",
    "apply_patch": "write",
    "Delete": "delete",
    "WebFetch": "web_fetch",
    "WebSearch": "web_search",
    "web_search": "web_search",
    "Agent": "subagent",
    "Task": "subagent",
    "TodoWrite": "todo",
    "TodoRead": "todo",
    "TaskGet": "task_read",
    "TaskList": "task_read",
    "TaskOutput": "task_read",
    "TaskCreate": "task_write",
    "TaskUpdate": "task_write",
    "TaskStop": "task_write",
    "ToolSearch": "discovery",
    "tool_search": "discovery",
    "Skill": "skill",
    "Workflow": "workflow",
    "EnterPlanMode": "plan_enter",
    "ExitPlanMode": "plan_exit",
    "AskUserQuestion": "question",
    "request_user_input": "question",
    "CodexEscalation": "escalation",
    "CodexTerminalInput": "escalation",
}

# Roles whose calls already have dedicated rich rendering (the question
# cards, the plan-mode rows, the task list's tool_info cards) — skip to
# avoid noise. A subagent's report is forwarded: the pump attaches it to the
# spawn block.
_SKIP_ROLES = {"question", "plan_enter", "plan_exit", "task_read", "task_write"}

# MCP tools to skip (display-mcp tools — the image/link itself just appeared)
_SKIP_MCP_TOOLS = {
    "mcp__display__display_image",
    "mcp__display__send_url",
    "mcp__display__send_file",
}


# The texts the CLI adds to an MCP result it hands the model: the pointer
# after an image and the line before a resource's picture (the proxy's
# ``services/mcp/result_files.py`` drops the same two).
_CLI_POINTER_RE = re.compile(
    r"^\[(Image: source: [^\]\n]*tool-results[/\\][^\]\n]*"
    r"|Resource from [^\]\n]* at [^\]\n]*)\]$")

# What a result's files may weigh on the way to the proxy (the proxy's caps
# decide; these keep the request inside its body limit): a block's base64,
# the blocks' base64 together, and their count.
# The MCP content block kinds that carry bytes (the MCP schema's words).
_IMAGE_BLOCK = "image"
_RESOURCE_BLOCK = "resource"
_FILE_B64_MAX = 20 * 1024 * 1024
_FILES_B64_MAX = 40 * 1024 * 1024
_FILES_MAX = 8
# The PostToolUse hook runs under a 10 s timeout; both requests fit in this.
_BUDGET_S = 8.5


def _text_blocks(blocks: list) -> str:
    """The text of an MCP result's content blocks, the CLI's own lines left
    out (an MCP tool's response is a LIST of blocks)."""
    parts = []
    for block in blocks:
        if isinstance(block, dict) and block.get("type") == "text":
            text = block.get("text", "")
            if isinstance(text, str) and text and not _CLI_POINTER_RE.match(text.strip()):
                parts.append(text)
    return "\n".join(parts)


def _binary_blocks(tool_result) -> list:
    """The image blocks and resource blobs of a list response, the large
    ones replaced by their size (the proxy names them in its line)."""
    if not isinstance(tool_result, list):
        return []
    out, total, sent = [], 0, 0
    for block in tool_result:
        if not isinstance(block, dict):
            continue
        kind = block.get("type")
        if kind == _IMAGE_BLOCK:
            src = block.get("source") if isinstance(block.get("source"), dict) else {}
            data = src.get("data") or block.get("data") or ""
            mime = src.get("media_type") or block.get("mimeType") or ""
        elif kind == _RESOURCE_BLOCK and isinstance(block.get(_RESOURCE_BLOCK), dict):
            data = block[_RESOURCE_BLOCK].get("blob") or ""
            mime = block[_RESOURCE_BLOCK].get("mimeType") or ""
        else:
            continue
        if not isinstance(data, str) or not data:
            continue
        if (sent >= _FILES_MAX or len(data) > _FILE_B64_MAX
                or total + len(data) > _FILES_B64_MAX):
            out.append({"type": kind, "mime": mime, "skipped_bytes": len(data) * 3 // 4})
            continue
        total += len(data)
        sent += 1
        out.append(block)
    return out


def _extract_result_text(tool_result: dict) -> str:
    """Extract the full text content from a tool result."""
    result_text = ""
    if isinstance(tool_result, list):
        result_text = _text_blocks(tool_result)
    elif isinstance(tool_result, dict):
        result_text = (
            tool_result.get("content", "")
            or tool_result.get("text", "")
            or tool_result.get("stdout", "")
            or ""
        )
        if isinstance(result_text, list):
            result_text = _text_blocks(result_text)
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
    """Extract a one-line summary from the tool result text, by role (the
    proxy's ``transcript_tool_events.result_summary`` is the same policy)."""
    role = TOOL_ROLES.get(tool_name, "")

    # A shell: show line count
    if role == "shell":
        lines = result_text.count("\n") + 1 if result_text.strip() else 0
        return f"{lines} lines" if lines else "ok"

    # A content search: match count
    if role == "search":
        if not result_text.strip():
            return "no matches"
        lines = [l for l in result_text.strip().splitlines() if l.strip()]
        return f"{len(lines)} results"

    # A glob: file count
    if role == "glob":
        if not result_text.strip():
            return "no files"
        lines = [l for l in result_text.strip().splitlines() if l.strip()]
        return f"{len(lines)} files"

    # A read: line count
    if role == "read":
        if not result_text.strip():
            return "empty file"
        lines = result_text.count("\n") + 1
        return f"{lines} lines"

    # A write: ok
    if role == "write":
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


_PATH_KEYS = ("file_path", "path", "notebook_path")


def _tool_paths(tool_input) -> list:
    """The path arguments of a native tool call (Read / Write / Edit / Glob /
    NotebookEdit name their target under one of three keys)."""
    if not isinstance(tool_input, dict):
        return []
    out = []
    for key in _PATH_KEYS:
        v = tool_input.get(key)
        if isinstance(v, str) and v and v not in out:
            out.append(v)
    return out


_COMMAND_MAX = 2048


def _tool_command(tool_name: str, tool_input) -> str:
    """A shell tool's command text, capped — the platform's record reads a
    commit, a push or a build from it. Empty for every other tool."""
    if TOOL_ROLES.get(tool_name, "") != "shell" or not isinstance(tool_input, dict):
        return ""
    cmd = tool_input.get("command")
    if not isinstance(cmd, str):
        return ""
    return cmd[:_COMMAND_MAX]


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
    raw = sys.stdin.read()
    # Codex app-server sessions (OTO_HOOK_NO_FORWARD set by the spawn): the
    # JSON-RPC stream already carries every tool result to the proxy, a
    # forward here would render each card twice. Checked before the parse:
    # this runs after every tool call of those sessions.
    if os.environ.get("OTO_HOOK_NO_FORWARD"):
        return
    try:
        inp = json.loads(raw)
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
    if TOOL_ROLES.get(tool_name, "") in _SKIP_ROLES or tool_name in _SKIP_MCP_TOOLS:
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
        # The paths the call named — the turn's record on the proxy
        # (session_events.post_tool); never the whole input (a Write's
        # content would ride along).
        "tool_paths": _tool_paths(tool_input),
        "tool_command": _tool_command(tool_name, tool_input),
    }).encode()

    started = time.monotonic()
    answer = _post(proxy_url, api_key, payload, timeout=5)

    # A tool whose results the platform keeps (the MCP's ``result_files``,
    # the proxy's answer says so): its pictures and file blobs go in a
    # second request, and the line naming where they were saved reaches the
    # agent as this hook's additionalContext.
    if not answer.get("result_files") or _is_error_result(tool_result, summary):
        return
    blocks = _binary_blocks(tool_result)
    if not blocks:
        return
    left = _BUDGET_S - (time.monotonic() - started)
    if left < 1:
        return
    payload = json.dumps({
        "session_id": session_id,
        "tool_name": tool_name,
        "tool_use_id": inp.get("tool_use_id", "") or "",
        "summary": "",
        "result_content": result_text[:4000],
        "files_only": True,
        "result_blocks": blocks,
    }).encode()
    # A socket timeout bounds each read, not the upload: the request runs in
    # a thread joined at the budget, and a late answer is dropped (the proxy
    # still keeps the files; the agent just is not told).
    kept: dict = {}
    sender = threading.Thread(
        target=lambda: kept.update(_post(proxy_url, api_key, payload, timeout=left)),
        daemon=True,
    )
    sender.start()
    sender.join(left)
    line = kept.get("additional_context")
    if not sender.is_alive() and isinstance(line, str) and line:
        print(json.dumps({"hookSpecificOutput": {
            "hookEventName": "PostToolUse", "additionalContext": line}}))


def _post(proxy_url: str, api_key: str, payload: bytes, *, timeout: float) -> dict:
    """POST to the tool-result route; the JSON answer, or {} on any
    failure (never interrupts Claude)."""
    req = urllib.request.Request(
        f"{proxy_url}/v1/hooks/tool-result",
        data=payload,
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = json.loads(resp.read() or b"{}")
            return body if isinstance(body, dict) else {}
    except Exception:
        return {}


if __name__ == "__main__":
    main()
