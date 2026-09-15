"""SessionManager — unified session pool and execution layer factory.

Creates the right ExecutionLayer based on agent config (execution_path).
Provides a single entry point for session lifecycle management.
"""

import logging
from typing import TYPE_CHECKING

from storage.agents import agent_store
from core.execution_layer import ExecutionLayer, LayerCapabilities
from core.layers.cli import CLIExecutionLayer
from core.layers.direct import DirectLLMExecutionLayer
from core.layers.codex import CodexCLIExecutionLayer

if TYPE_CHECKING:
    from core.remote.remote_execution import RemoteExecutionLayer

logger = logging.getLogger("claude-proxy")


# ---------------------------------------------------------------------------
# Singleton execution layer instances
# ---------------------------------------------------------------------------

_cli_layer = CLIExecutionLayer()
_direct_layer = DirectLLMExecutionLayer()
_codex_layer = CodexCLIExecutionLayer()

_LAYERS: dict[str, ExecutionLayer] = {
    "claude-code-cli": _cli_layer,
    "direct-llm": _direct_layer,
    "codex-cli": _codex_layer,
}

# Remote execution layer — initialized lazily on first use
_remote_layer: "RemoteExecutionLayer | None" = None


def _get_remote_layer() -> "RemoteExecutionLayer":
    """Lazily create the RemoteExecutionLayer singleton."""
    global _remote_layer
    if _remote_layer is None:
        from core.remote.remote_execution import RemoteExecutionLayer
        from core.remote.satellite_connection import get_connection_manager
        _remote_layer = RemoteExecutionLayer(get_connection_manager())
    return _remote_layer


def is_session_registered(session_id: str) -> bool:
    """True while ``session_id`` is a LIVE session in some execution layer's
    registry (cli / codex / direct / remote). This — not the persisted
    security context — is the liveness a session-scoped token is checked
    against (``middleware.external_session_confinement``): the registries
    are populated at spawn and popped at close, so a token lifted from a
    session is dead the moment the session ends."""
    if not session_id:
        return False
    from core.layers.cli.session import _persistent_sessions
    from core.layers.codex.session import _codex_sessions
    from core.layers.direct.session import _direct_sessions
    if (
        session_id in _persistent_sessions
        or session_id in _codex_sessions
        or session_id in _direct_sessions
    ):
        return True
    remote = _remote_layer
    return bool(remote is not None and session_id in getattr(remote, "_sessions", {}))


# ---------------------------------------------------------------------------
# SessionManager
# ---------------------------------------------------------------------------

def get_execution_layer(
    agent_name: str,
    execution_path: str = "",
    user_sub: str | None = None,
    role: str = "manager",
    execution_target: str = "",
) -> ExecutionLayer:
    """Return the ExecutionLayer for an agent based on its execution_path.

    If execution_path override is provided, uses that instead of the agent's
    DB setting. Falls back to CLI layer if not found.

    Remote targeting: resolves effective target via user override > agent
    default > local, respecting viewer-on-admin-remote fallback and offline
    fallback flags. Direct LLM always runs locally (API calls, no subprocess).

    `execution_target` lets a caller that already resolved the target while
    building its AgentConfig pass it through, so the layer can never disagree
    with the config (the divergence that silently ran user-scoped tasks /
    meetings / phone on the wrong layer). When provided we skip RE-resolution
    but STILL enforce the per-user isolation guards below against it.
    """
    agent = agent_store.get_agent(agent_name)

    if not execution_path:
        if not agent:
            logger.warning(f"Agent '{agent_name}' not found, defaulting to CLI layer")
            return _cli_layer
        execution_path = agent.get("execution_path", "claude-code-cli")

    # Direct LLM always local (API calls, no subprocess to remote)
    if execution_path == "direct-llm":
        return _direct_layer

    # Resolve effective target unless the caller already resolved one while
    # building its config. Returns (target, fallback_reason). Skipping
    # re-resolution avoids re-running user/role logic the caller may not
    # replicate; the isolation guards below run on the passed target either way.
    from storage import remote_store
    if not execution_target:
        execution_target, _reason = remote_store.resolve_execution_target(
            agent_name, user_sub, role,
        )
    if execution_target.startswith("__offline__:"):
        # Resolver decided the intended remote target is unreachable and no
        # fallback is allowed. Hard-fail here so we never silently run on the
        # wrong machine (different MCPs, different filesystem, etc.). Callers
        # that legitimately need to operate on offline agents (e.g. shutdown
        # close_session) wrap this in try/except.
        offline_machine_id = execution_target.removeprefix("__offline__:")
        raise RuntimeError(
            f"Agent '{agent_name}' targets remote machine "
            f"{offline_machine_id[:8]} which is offline. Bring the satellite "
            f"back online or change the agent's execution target."
        )
    if execution_target != "local":
        # Per-user satellite isolation: agent-scope sessions
        # (scheduled tasks, phone, triggers — no user_sub) must never run
        # on user-paired machines, which have only ONE user's data and no
        # service-account credential surface. Refusal here fails the
        # session start with a clear error instead of silently routing to
        # a machine that can't serve the session.
        machine = remote_store.get_remote_machine(execution_target)
        if machine and (machine.get("pairing_scope") or "") != "admin":
            # `not user_sub` (NOT `is None`) — `pick_account` treats both None
            # AND "" as service-scope, so a service-account session with an empty
            # user_sub must also be refused here, else its service-account
            # credentials (GH_TOKEN, MCP bearer, …) would land on a user-owned
            # disk. Fail-closed: a real user-scope session always has a truthy sub.
            if not user_sub:
                raise RuntimeError(
                    f"Sessions with no user identity (agent-scope tasks, phone, "
                    f"triggers) cannot run on user-paired remote machines. Agent "
                    f"'{agent_name}' is routed to machine {execution_target[:8]} "
                    f"owned by {machine.get('registered_by', '?')[:16]} "
                    f"(pairing_scope={machine.get('pairing_scope') or 'unknown'}); "
                    f"only admin-shared machines may host them. (A user-scope task "
                    f"should carry its creator's user_sub — if you see this for one, "
                    f"its identity wasn't threaded to the execution layer.)"
                )
            # Defense-in-depth — if admin disabled user-paired
            # machines mid-session (and the cascade somehow missed this row),
            # refuse to start the session here too.
            from storage import database as _db
            if _db.get_platform_setting("allow_user_paired_machines") == "0":
                raise RuntimeError(
                    "User-paired remote machines are disabled by admin "
                    "policy. Agent will run locally instead."
                )
        return _get_remote_layer()

    path = execution_path
    layer = _LAYERS.get(path)
    if not layer:
        logger.warning(
            f"Unknown execution_path '{path}' for agent '{agent_name}', "
            f"defaulting to CLI layer"
        )
        return _cli_layer

    return layer


def resolve_execution_path(agent_name: str, execution_path: str = "") -> str:
    """Resolve the actual execution_path for an agent (ignoring remote routing).

    Returns 'claude-code-cli', 'codex-cli', or 'direct-llm' — never 'remote'.
    Used by callers that need the path for config building or DB storage.
    """
    if execution_path:
        return execution_path
    agent = agent_store.get_agent(agent_name)
    return (agent or {}).get("execution_path", "claude-code-cli")


def get_layer_by_path(execution_path: str) -> ExecutionLayer:
    """Return the ExecutionLayer for a given execution_path string.

    Useful when the caller already knows the path (e.g. phone server).
    """
    return _LAYERS.get(execution_path, _cli_layer)


def register_layer(execution_path: str, layer: ExecutionLayer) -> None:
    """Register a new execution layer (for plugins / future layers)."""
    _LAYERS[execution_path] = layer
    logger.info(f"Registered execution layer: {execution_path} -> {type(layer).__name__}")


def get_all_layers() -> dict[str, ExecutionLayer]:
    """Return all registered execution layers."""
    return dict(_LAYERS)


def get_layer_capabilities(execution_path: str) -> LayerCapabilities | None:
    """Return the LayerCapabilities for a given execution_path."""
    layer = _LAYERS.get(execution_path)
    return layer.capabilities if layer else None


def get_all_capabilities() -> dict[str, dict]:
    """Return serialized capabilities for all registered layers.

    Used by the /v1/execution-layers API endpoint.
    """
    return {
        path: layer.capabilities.to_dict()
        for path, layer in _LAYERS.items()
    }
