"""Direct LLM execution layer -- multi-provider API calls with MCP tools."""

from core.layers.direct.mcp import AgentMCPManager, MCPPool, mcp_pool
from core.layers.direct.session import (
    DirectSession,
    create_direct_session,
    get_direct_session,
    close_direct_session,
    run_direct_stream,
    reap_idle_direct_sessions,
)
from core.layers.direct.layer import (
    DirectLLMExecutionLayer,
    direct_event_to_common,
)

__all__ = [
    "AgentMCPManager",
    "MCPPool",
    "mcp_pool",
    "DirectSession",
    "create_direct_session",
    "get_direct_session",
    "close_direct_session",
    "run_direct_stream",
    "reap_idle_direct_sessions",
    "DirectLLMExecutionLayer",
    "direct_event_to_common",
]
