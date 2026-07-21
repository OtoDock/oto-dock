"""CLI execution layer -- Claude Code subprocess management."""

from core.layers.cli.helpers import (
    ClaudeStreamChunk,
    abort_session,
    _build_env,
    _build_client_context,
    _kill_process,
    _extract_context_window,
    _extract_turn_context,
    _extract_tool_summary,
    _SKIP_INLINE_TOOLS,
)
from core.layers.cli.session import (
    PersistentSession,
    get_persistent_session,
    get_or_create_persistent_session,
    close_persistent_session,
    abort_persistent_session,
    interrupt_persistent_session,
    reap_idle_sessions,
    _persistent_sessions,
)
from core.layers.cli.layer import (
    CLIExecutionLayer,
    cli_chunk_to_events,
)

__all__ = [
    "ClaudeStreamChunk",
    "abort_session",
    "_build_env",
    "_build_client_context",
    "_kill_process",
    "_extract_context_window",
    "_extract_turn_context",
    "_extract_tool_summary",
    "_SKIP_INLINE_TOOLS",
    "PersistentSession",
    "get_persistent_session",
    "get_or_create_persistent_session",
    "close_persistent_session",
    "abort_persistent_session",
    "interrupt_persistent_session",
    "reap_idle_sessions",
    "_persistent_sessions",
    "CLIExecutionLayer",
    "cli_chunk_to_events",
]
