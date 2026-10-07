"""SessionManager — unified session pool and execution layer factory.

Creates the right ExecutionLayer based on agent config (execution_path).
Provides a single entry point for session lifecycle management.
"""

import logging
from typing import TYPE_CHECKING

from storage.agents import agent_store
from core import placement
from core.execution_layer import (
    DEFAULT_EXECUTION_PATH, ExecutionLayer, LayerCapabilities,
)
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

class UnknownExecutionPath(ValueError):
    """An ``execution_path`` no layer is registered for.

    Deliberately NOT a ``RuntimeError``: ``services/scheduler/delivery.py``
    and ``ws/duplex_attach.py`` already catch RuntimeError from this module
    and read it as "the remote target is offline" — a different, benign
    condition. A misconfigured engine must not masquerade as one.
    """


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


def valid_execution_paths() -> set[str]:
    """Every engine id an agent may be configured with.

    THE source of truth — the registry itself. API validation, the checks
    document parser and the admin endpoints all read this instead of retyping
    the id set, so registering a layer is the only step that makes its id
    valid anywhere.

    The REMOTE layer is deliberately absent: ``remote`` is a placement, not an
    engine — a remote session still runs claude-code-cli or codex-cli.
    """
    return set(_LAYERS)


def _iter_session_holders() -> list[ExecutionLayer]:
    """Every layer that can hold a session record — the three local layers
    plus the remote layer WHEN it exists.

    The remote layer is not in ``_LAYERS`` (it is a placement, not an engine)
    but it does hold session records, so any question of the form "does some
    layer know this session" must include it. Never CREATES the remote layer:
    before the first remote session it legitimately has none, and building it
    here would drag the satellite connection manager into import paths that
    do not need it.
    """
    layers: list[ExecutionLayer] = list(_LAYERS.values())
    if _remote_layer is not None:
        layers.append(_remote_layer)
    return layers


def find_layer_for_session(session_id: str) -> ExecutionLayer | None:
    """The layer holding ``session_id``, or None.

    Replaces the hand-written probes that imported each layer's private pool
    dict in turn. Remote is checked FIRST, matching what those probes did: a
    remote session's id can also appear in no local pool, and asking the
    local layers first only wastes three dict lookups.
    """
    if not session_id:
        return None
    if _remote_layer is not None and _remote_layer.owns_session(session_id):
        return _remote_layer
    for layer in _LAYERS.values():
        if layer.owns_session(session_id):
            return layer
    return None


def engine_layer_for_session(session_id: str) -> ExecutionLayer | None:
    """The layer of the ENGINE behind a live session — never the placement.

    An interactive PTY session lives in ``core.session.interactive_session``
    (no pool holds it), so that registry is asked first, by its execution
    path; a pooled session answers through its holder (a remote session →
    the engine the satellite runs, via the placement's ``capabilities_for``).
    None for an id nothing holds: the permission authority reads that as
    fail-closed."""
    if not session_id:
        return None
    from core.session import interactive_session
    isess = interactive_session.get(session_id)
    if isess is not None:
        return _LAYERS.get(isess.execution_path)
    holder = find_layer_for_session(session_id)
    if holder is None:
        return None
    return _LAYERS.get(holder.capabilities_for(session_id).name)


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
            # The agent row is gone but a session of it may still be live —
            # a delete mid-session, or a shutdown sweep walking a pool. The
            # platform default is the right guess (it is what the row would
            # have said) and the caller's job here is almost always to CLOSE
            # something, which must not raise.
            logger.warning(
                f"Agent '{agent_name}' not found, assuming "
                f"{DEFAULT_EXECUTION_PATH}"
            )
            execution_path = DEFAULT_EXECUTION_PATH
        else:
            execution_path = agent.get("execution_path") or DEFAULT_EXECUTION_PATH

    # An engine that cannot run on a satellite (Direct LLM: in-process API
    # calls, nothing to relocate) is always local — no target resolution.
    local_layer = _LAYERS.get(execution_path)
    if local_layer is not None and not local_layer.capabilities.runtime.supports_remote_execution:
        return local_layer

    # Resolve effective target unless the caller already resolved one while
    # building its config. Returns (target, fallback_reason). Skipping
    # re-resolution avoids re-running user/role logic the caller may not
    # replicate; the isolation guards below run on the passed target either way.
    from storage import remote_store
    if not execution_target:
        execution_target, _reason = remote_store.resolve_execution_target(
            agent_name, user_sub, role,
        )
    if placement.is_offline_sentinel(execution_target):
        # Resolver decided the intended remote target is unreachable and no
        # fallback is allowed. Hard-fail here so we never silently run on the
        # wrong machine (different MCPs, different filesystem, etc.). Callers
        # that legitimately need to operate on offline agents (e.g. shutdown
        # close_session) wrap this in try/except.
        offline_machine_id = placement.offline_machine_of(execution_target)
        raise RuntimeError(
            f"Agent '{agent_name}' targets remote machine "
            f"{offline_machine_id[:8]} which is offline. Bring the satellite "
            f"back online or change the agent's execution target."
        )
    if not placement.is_local(execution_target):
        # Per-user satellite isolation: agent-scope sessions
        # (scheduled tasks, phone, triggers — no user_sub) must never run
        # on user-paired machines, which have only ONE user's data and no
        # service-account credential surface. Refusal here fails the
        # session start with a clear error instead of silently routing to
        # a machine that can't serve the session.
        machine = remote_store.get_remote_machine(execution_target)
        if machine and not placement.machine_is_admin_paired(machine):
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

    layer = _LAYERS.get(execution_path)
    if not layer:
        # FAIL CLOSED. This used to fall back to the CLI layer, which meant a
        # typo'd or retired engine id ran the agent on Claude Code — the wrong
        # binary, the wrong credentials, the wrong sandbox, silently. An id
        # that no layer claims is a configuration error, and the caller must
        # see it.
        raise UnknownExecutionPath(
            f"Agent '{agent_name}' is configured with execution_path "
            f"'{execution_path}', which no AI engine is registered for. "
            f"Known engines: {', '.join(sorted(_LAYERS))}."
        )

    return layer


def resolve_execution_path(agent_name: str, execution_path: str = "") -> str:
    """Resolve the actual execution_path for an agent (ignoring remote routing).

    Returns one of the registered engine ids — never 'remote'. Used by callers
    that need the path for config building or DB storage.
    """
    if execution_path:
        return execution_path
    agent = agent_store.get_agent(agent_name)
    return (agent or {}).get("execution_path") or DEFAULT_EXECUTION_PATH


def get_layer_by_path(execution_path: str) -> ExecutionLayer:
    """Return the ExecutionLayer for a given execution_path string.

    Useful when the caller already knows the path (e.g. phone server). Raises
    ``UnknownExecutionPath`` for an unregistered id — see get_execution_layer.
    """
    layer = _LAYERS.get(execution_path)
    if not layer:
        raise UnknownExecutionPath(
            f"No AI engine is registered for execution_path "
            f"'{execution_path}'. Known engines: {', '.join(sorted(_LAYERS))}."
        )
    return layer


def register_layer(execution_path: str, layer: ExecutionLayer) -> None:
    """Register a new execution layer (for plugins / future layers)."""
    _LAYERS[execution_path] = layer
    logger.info(f"Registered execution layer: {execution_path} -> {type(layer).__name__}")


def get_all_layers() -> dict[str, ExecutionLayer]:
    """Return all registered execution layers."""
    return dict(_LAYERS)


def get_layer_capabilities(execution_path: str) -> LayerCapabilities | None:
    """Return the LayerCapabilities for a given execution_path, or None.

    The tolerant form, for pure QUERIES that must not raise (placement
    resolution, the prompt roster, a machine card) — an unknown id reads as
    "supports nothing". Builders and spawn paths use ``capabilities_for_path``
    below, which fails closed like ``get_execution_layer``.
    """
    layer = _LAYERS.get(execution_path)
    return layer.capabilities if layer else None


def capabilities_for_path(execution_path: str) -> LayerCapabilities:
    """The descriptor for ``execution_path``; raises ``UnknownExecutionPath``
    for an id no layer claims. The config builders read the engine's facts
    through this so a bogus stored id fails at the same point, with the same
    message, as the layer lookup that follows."""
    return get_layer_by_path(execution_path).capabilities


def account_label_for(execution_path: str, default: str = "") -> str:
    """What a subscription to this engine is called in a message to its
    owner ("Claude", "ChatGPT" — ``identity.account_label``), or ``default``
    for an engine that is not registered. For the sweeps and the pool caps,
    which name the vendor product in notifications and refusals and must not
    raise on a stored row whose engine has since gone."""
    caps = get_layer_capabilities(execution_path)
    return (caps.identity.account_label if caps else "") or default


def get_all_capabilities() -> dict[str, dict]:
    """Return serialized capabilities for all registered layers.

    Used by the /v1/execution-layers API endpoint.
    """
    return {
        path: layer.capabilities.to_dict()
        for path, layer in _LAYERS.items()
    }
