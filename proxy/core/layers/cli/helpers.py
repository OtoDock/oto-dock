"""Shared helpers of the Claude Code engine: the stream chunk, the session
environment and client context of a spawn, and what the translator reads
off a result (tool summaries, the context window, the turn's context).
"""

import logging
import re
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pathlib import Path

logger = logging.getLogger("otodock")


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------


@dataclass
class ClaudeStreamChunk:
    """A single chunk from a streaming Claude call."""

    text: str = ""
    is_done: bool = False
    session_id: str = ""
    is_error: bool = False
    error_message: str = ""
    event_type: str = "text"  # text | thinking | tool_start | tool_end | tool_info | task_spawn | subagent_end | workflow_started | workflow_progress | workflow_ended | delegate_spawn | metadata | system | plan_mode | permission_prompt | todo_update | rate_limit
    event_data: dict = field(default_factory=dict)


# Tools whose inline display is handled elsewhere or not useful
_SKIP_INLINE_TOOLS = {"AskUserQuestion", "EnterPlanMode", "ExitPlanMode"}


def _extract_tool_summary(name: str, tool_input: dict) -> str:
    """Extract a one-line summary from tool input for inline display."""
    if name in ("Read", "Edit", "Write"):
        fp = tool_input.get("file_path", "")
        # Show just filename for brevity
        return fp.rsplit("/", 1)[-1] if "/" in fp else fp
    if name == "Bash":
        # The model-written `description` says WHAT the command does — that's
        # the collapsed-pill title; the command itself lives in the expanded
        # view. Fall back to the command when no description was given.
        cmd = tool_input.get("description", "") or tool_input.get("command", "")
        return cmd[:100] + "..." if len(cmd) > 100 else cmd
    if name == "Grep":
        parts = []
        pattern = tool_input.get("pattern", "")
        if pattern:
            parts.append(f'"{pattern}"')
        path = tool_input.get("path", "")
        if path:
            parts.append(f"in {path.rsplit('/', 1)[-1] if '/' in path else path}")
        return " ".join(parts)
    if name == "Glob":
        return tool_input.get("pattern", "")
    if name == "WebSearch":
        return tool_input.get("query", "")
    if name == "WebFetch":
        return tool_input.get("url", "")
    # Generic: try common parameter names (works for MCP tools too)
    for key in ("command", "query", "pattern", "file_path", "url", "name"):
        val = tool_input.get(key)
        if val and isinstance(val, str):
            return val[:100] + "..." if len(val) > 100 else val
    return ""


# ---------------------------------------------------------------------------
# Command building
# ---------------------------------------------------------------------------


def _build_client_context(mcp_config_path: "Path | None", client_type: str) -> str:
    """Build client-specific context to append to the system prompt.

    Delegates to the registered adapter for the given client_type.
    Returns empty string if no adapter is registered (e.g. task sessions).
    """
    from adapters import get_adapter
    adapter = get_adapter(client_type)
    if adapter is None:
        return ""
    return adapter.build_client_context(mcp_config_path)




def _build_env(session_id: str,
               credential_env: dict[str, str] | None = None,
               agent_name: str = "",
               username: str = "",
               user_role: str = "") -> dict[str, str]:
    """Build a minimal, secure environment for the Claude subprocess.

    Delegates to the shared env_builder which provides allowlisted vars,
    a session-scoped JWT token instead of the master PROXY_API_KEY, and
    the standard OTO_* env vars for community-MCP scope-aware behavior.
    """
    from core.sandbox.env_builder import build_session_env
    return build_session_env(
        session_id, agent_name, credential_env,
        username=username, user_role=user_role,
    )


# ---------------------------------------------------------------------------
# Context window extraction helpers
# ---------------------------------------------------------------------------


def _extract_context_window(data: dict) -> int:
    """Extract the largest contextWindow from CLI result's modelUsage field."""
    model_usage = data.get("modelUsage", {})
    max_window = 0
    for _model_id, usage in model_usage.items():
        window = usage.get("contextWindow", 0)
        if window > max_window:
            max_window = window
    return max_window


def _extract_turn_context(event: dict) -> int:
    """Extract per-turn input token count from a message_start streaming event.

    The message_start event contains per-API-call usage (not cumulative),
    so input_tokens + cache tokens = current context window usage for that turn.
    """
    msg = event.get("message", {})
    usage = msg.get("usage", {})
    return (
        usage.get("input_tokens", 0)
        + usage.get("cache_read_input_tokens", 0)
        + usage.get("cache_creation_input_tokens", 0)
    )


# claude-code#63943: a graceful interrupt mid-thinking can persist an unsigned
# thinking block in the CLI's session history; every later API call then 400s
# on the block's missing/invalid signature. Matched against the error result
# of the first turn(s) after an interrupt to trigger the killpg+resume repair.
_THINKING_SIG_RE = re.compile(
    r"thinking.{0,120}signature|signature.{0,120}thinking", re.I | re.S,
)

# After a steered turn's result: how long to wait for the init of the turn
# the CLI starts by itself from a frame it did not consume mid-turn.
_STEER_INIT_GRACE_S = 3.0
