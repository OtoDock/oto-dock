"""SessionManager — unified session pool and execution layer factory.

Creates the right ExecutionLayer based on agent config (execution_path).
Provides a single entry point for session lifecycle management.
"""

import logging
import threading
from dataclasses import dataclass
from typing import TYPE_CHECKING

from core import placement
from core.engine import Engine
from core.execution_layer import ExecutionLayer, LayerCapabilities
from core.layers.cli import CLIExecutionLayer
from core.layers.codex import CodexCLIExecutionLayer
from core.layers.direct import DirectLLMExecutionLayer
from core.placement import PlacementCapabilities
from storage.agents import agent_store
from storage.agents.schema import DEFAULT_EXECUTION_PATH

if TYPE_CHECKING:
    from core.remote.remote_execution import RemoteExecutionLayer

logger = logging.getLogger("otodock")


# ---------------------------------------------------------------------------
# Singleton execution layer instances
# ---------------------------------------------------------------------------

_cli_layer = CLIExecutionLayer()
_direct_layer = DirectLLMExecutionLayer()
_codex_layer = CodexCLIExecutionLayer()

#: The engines, by id: what each one is (``Engine``). No session is asked
#: of this table: who holds a session is ``_iter_session_holders``.
_ENGINES: dict[str, Engine] = {
    "claude-code-cli": _cli_layer,
    "direct-llm": _direct_layer,
    "codex-cli": _codex_layer,
}


def holding_engines() -> dict[str, ExecutionLayer]:
    """The engines that hold sessions in the proxy itself, by id: Direct
    LLM's layer alone (in-process API calls, on no machine). Every Claude
    Code and Codex session is the remote layer's."""
    return {path: engine for path, engine in _ENGINES.items() if isinstance(engine, ExecutionLayer)}


class UnknownExecutionPath(ValueError):
    """An ``execution_path`` no layer is registered for.

    Deliberately NOT a ``RuntimeError``: ``services/scheduler/delivery.py``
    and ``ws/duplex_attach.py`` already catch RuntimeError from this module
    and read it as "the remote target is offline" — a different, benign
    condition. A misconfigured engine must not masquerade as one.
    """


# Remote execution layer — initialized lazily on first use
_remote_layer: "RemoteExecutionLayer | None" = None
_remote_layer_lock = threading.Lock()


def _get_remote_layer() -> "RemoteExecutionLayer":
    """Lazily create the RemoteExecutionLayer singleton. Callers run on the
    loop and on store threads: a second instance would hold sessions
    ``owns_session`` never sees."""
    global _remote_layer
    if _remote_layer is not None:
        return _remote_layer
    from core.remote.remote_execution import RemoteExecutionLayer
    from core.remote.satellite_connection import get_connection_manager
    with _remote_layer_lock:
        if _remote_layer is None:
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
    return set(_ENGINES)


def _iter_session_holders() -> list[ExecutionLayer]:
    """Every layer that holds session records: the engines that keep their
    own in the proxy (Direct LLM) plus the remote layer WHEN it exists,
    which holds every session of a process engine.

    The remote layer is not an engine (it is a placement), so any question
    of the form "does some layer know this session" asks here, never the
    engines' table. Never CREATES the remote layer: before the first remote
    session it legitimately has none, and building it here would drag the
    satellite connection manager into import paths that do not need it.
    """
    layers: list[ExecutionLayer] = list(holding_engines().values())
    if _remote_layer is not None:
        layers.append(_remote_layer)
    return layers


def find_layer_for_session(session_id: str) -> ExecutionLayer | None:
    """The layer holding ``session_id``, or None.

    Replaces the hand-written probes that imported each layer's private pool
    dict in turn. Remote is checked FIRST: nearly every session is on a
    machine.
    """
    if not session_id:
        return None
    if _remote_layer is not None and _remote_layer.owns_session(session_id):
        return _remote_layer
    for layer in holding_engines().values():
        if layer.owns_session(session_id):
            return layer
    return None


@dataclass(frozen=True)
class LiveSession:
    """A session some layer holds: that layer, the machine it is on (``""``
    when the proxy holds it), whether that machine's socket is up, and
    whether its reconnect grace holds it. Whether its process is alive is
    the layer's own answer (``is_session_alive``)."""

    layer: ExecutionLayer
    machine_id: str = ""
    linked: bool = True
    grace: bool = False

    @property
    def reachable(self) -> bool:
        """Its machine answers, or is coming back within its grace."""
        return self.linked or self.grace


def live_session(session_id: str) -> LiveSession | None:
    """Is the session live, and on which layer: the one lookup beside
    ``find_layer_for_session`` (who holds it), with the machine it is on
    and that machine's link. Single-key reads only, safe from a worker
    thread, and it never creates the remote layer."""
    holder = find_layer_for_session(session_id)
    if holder is None:
        return None
    remote = _remote_layer
    if remote is None or holder is not remote:
        return LiveSession(holder)
    machine = remote.machine_of_session(session_id)
    from core.remote.satellite_connection import get_connection_manager
    return LiveSession(holder, machine,
                       linked=bool(machine) and get_connection_manager().is_connected(machine),
                       grace=remote.is_session_grace_held(session_id))


def compute_placement_for(execution_path: str, *, prefer: str = "",
                          shared_folder: bool = False, agent: str = "") -> PlacementCapabilities | None:
    """The compute placement a session of ``execution_path`` would be built
    with now (``remote_status.serving_compute_machine``, with the chat's own
    machine as ``prefer`` and the session's ``agent``), None when no compute
    machine can take it: the builders then refuse the session
    (``remote_status.compute_row_for``). For a caller that has no built
    config and must agree with what the builders would build. Naming
    ``agent`` may read the store: off the loop."""
    from services.remote import remote_status
    row = remote_status.serving_compute_machine(execution_path, prefer=prefer, shared_folder=shared_folder,
                                                **({"agent": agent} if agent else {}))
    if row is None:
        return None
    return placement.from_machine(placement.KIND_COMPUTE, row)


def own_copy_machine_of(session_id: str) -> str:
    """The compute machine with its own copy that holds a live session,
    ``""`` for one held anywhere else: what a chat that adopts the session
    records (``remote_store.set_chat_compute_machine``)."""
    live = live_session(session_id) if session_id else None
    if live is None or not live.machine_id:
        return ""
    from core.remote.satellite_connection import get_connection_manager
    return live.machine_id if get_connection_manager().owns_copy(live.machine_id) else ""


def record_chat_place(chat_id: str, session_id: str) -> None:
    """Record on a chat where the session it now runs is
    (``chats.compute_machine``, read back by ``remote_status.chat_place``):
    the compute machine with its own copy that holds it, else ``""``. For a
    door that binds a chat to a session it did not start on a known machine
    (a wake's fresh session, an adopted pre-warm, a reused warm one, a start
    in the platform's own sandbox); the remote start records the machine it
    started on. Synchronous: callers run it on the store's executor."""
    if not chat_id:
        return
    from storage import remote_store
    remote_store.set_chat_compute_machine(chat_id, own_copy_machine_of(session_id))


def _is_compute(machine_id: str) -> bool:
    from core.remote.satellite_connection import get_connection_manager
    return get_connection_manager().is_compute(machine_id)


def _compute_holds(live: LiveSession, session_id: str) -> bool:
    """Whether a compute machine holds the live session: one that is linked,
    or one in its reconnect grace that the session was placed on (the
    connection knows a machine's kind only while it is linked, the session's
    registered placement keeps it)."""
    if _is_compute(live.machine_id):
        return True
    if not live.grace:
        return False
    from core.session import session_state
    ctx = session_state.get_session_security(session_id)
    pl = getattr(ctx, "placement", None) if ctx is not None else None
    return bool(pl is not None and pl.kind == placement.KIND_COMPUTE and pl.machine_id == live.machine_id)


def no_compute_for(chat: dict, *, held: bool = True) -> bool:
    """Whether the chat is the platform's and no compute machine can take a
    session of it now: what its builders would refuse
    (``remote_status.compute_row_for``). A chat on a paired computer, and
    one whose session is live (a layer holds it, a machine that reconnects
    included, or its terminal runs), answer for themselves. ``held=False``
    asks for the machine alone: a session a machine that is away still
    holds does not answer for the chat (a spoken turn, which cannot wait
    for that machine). Synchronous: callers run it on the store's
    executor."""
    from core.session import interactive_registry
    from services.remote import remote_status
    if not placement.is_local(chat.get("execution_target")):
        return False
    sid = chat.get("session_id") or ""
    if held and sid and live_session(sid) is not None:
        return False
    if held and interactive_registry.find_live_for_chat(chat.get("id") or "") is not None:
        return False
    place = remote_status.chat_place(chat)
    agent = chat.get("agent") or ""
    path = resolve_execution_path(agent, chat.get("execution_path") or "")
    return compute_placement_for(path, prefer=place.machine, shared_folder=place.shared,
                                 agent=place.agent_for(agent)) is None


def layer_for_chat(agent: str, chat: dict | None, *, user_sub: str | None, role: str) -> ExecutionLayer:
    """The layer a chat's session runs on, from its stored place: the place
    it is pinned to, an empty pin resolved now (a chat whose machine was
    removed, one no warm-up has placed). The one reading of a chat's pin for
    its layer: for a machine pin, pin-first (a holder-first answer would
    send a turn to a stale process after a move whose close failed); for
    the platform's place, the holder of the chat's live session when the
    proxy or a compute machine holds it (a compute machine that is
    reconnecting still holds it: its turn waits for the link), else the
    layer the builders would build now. With no compute machine to take a
    session of the platform's place this is still the remote layer, where
    the chat's session is not alive: a chat is opened and probed with the
    machine away, and a start is refused by its build
    (``placement.NoComputeMachine``, ``no_compute_for``)."""
    row = chat or {}
    pin = row.get("execution_target") or ""
    path = row.get("execution_path") or ""
    built: PlacementCapabilities | None = None
    if placement.is_local(pin):
        from services.remote import remote_status
        sid = row.get("session_id") or ""
        live = live_session(sid) if sid else None
        if live is not None and (not live.machine_id or _compute_holds(live, sid)):
            return live.layer
        place = remote_status.chat_place(row)
        built = compute_placement_for(resolve_execution_path(agent, path),
                                      prefer=place.machine, shared_folder=place.shared)
    try:
        return get_execution_layer(
            agent, execution_path=path, user_sub=user_sub, role=role, execution_target=pin,
            **({"placement_built": built} if built is not None else {}),
        )
    except placement.NoComputeMachine:
        return _get_remote_layer()


def layer_for_prewarm(session_id: str, execution_path: str) -> ExecutionLayer | None:
    """The layer to close a pre-warmed session on: the one holding it, None
    when nobody does (a pool drops a dead entry on any lookup). The caller
    then gives back what the close would have (``release_unheld``).
    A pre-warm runs on the platform's place: on the compute machine that
    serves it (held by the remote layer), or in the proxy for Direct LLM; a
    personal or admin machine and an interactive session are never
    pre-warmed."""
    return find_layer_for_session(session_id)


def release_unheld(session_id: str) -> None:
    """What a session nobody holds any more still owns, given back: its
    permission state, the broker's secrets, its subscription seat and its
    slot. The work a layer's ``close_session`` does after its own pool's
    close, for a caller whose session has no layer left to close it on (a
    pre-warm a pool dropped, a victim that ended by itself)."""
    from core.concurrency import release_chat_slot
    from core.credentials import mcp_broker
    from core.session.session_state import cleanup_session_permission_state
    from services.engines.subscription_pool import release_subscription
    cleanup_session_permission_state(session_id)
    mcp_broker.purge_session(session_id)
    release_subscription(session_id)
    release_chat_slot(session_id)


def engine_layer_for_session(session_id: str) -> Engine | None:
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
        return _ENGINES.get(isess.execution_path)
    holder = find_layer_for_session(session_id)
    if holder is None:
        return None
    return _ENGINES.get(holder.capabilities_for(session_id).name)


# ---------------------------------------------------------------------------
# SessionManager
# ---------------------------------------------------------------------------

def get_execution_layer(
    agent_name: str,
    execution_path: str = "",
    user_sub: str | None = None,
    role: str = "manager",
    execution_target: str = "",
    placement_built: PlacementCapabilities | None = None,
) -> ExecutionLayer:
    """Return the ExecutionLayer for an agent based on its execution_path.

    If execution_path override is provided, uses that instead of the agent's
    DB setting. An engine id no layer claims raises ``UnknownExecutionPath``.

    Remote targeting: resolves effective target via user override > agent
    default > local, respecting viewer-on-admin-remote fallback and offline
    fallback flags. Direct LLM always runs locally (API calls, no subprocess).

    `execution_target` lets a caller that already resolved the target while
    building its AgentConfig pass it through, so the layer can never disagree
    with the config (the divergence that silently ran user-scoped tasks /
    meetings / phone on the wrong layer). When provided we skip RE-resolution
    but STILL enforce the per-user isolation guards below against it.

    ``placement_built`` is the config's own placement: a compute placement
    answers the remote layer (the compute machine serves the platform's
    place, the target stays ``local``). This function never asks whether a
    compute machine serves: the builders decide that once, and a caller with
    a config passes what they built. A process engine's session resolved to
    the platform's place with no compute placement has no layer:
    ``placement.NoComputeMachine``, the refusal the builders raise too.
    """
    agent = agent_store.get_agent(agent_name)

    if not execution_path:
        if not agent:
            # The agent row is gone but a session of it may still be live —
            # a delete mid-session, or a shutdown sweep walking a pool. The
            # platform default is the right guess (it is what the row would
            # have said). A caller that only closes something asks who holds
            # the session (``find_layer_for_session``) before it asks here.
            logger.warning(
                "Agent '%s' not found, assuming "
                "%s", agent_name, DEFAULT_EXECUTION_PATH
            )
            execution_path = DEFAULT_EXECUTION_PATH
        else:
            execution_path = agent.get("execution_path") or DEFAULT_EXECUTION_PATH

    # An engine that cannot run on a satellite (Direct LLM: in-process API
    # calls, nothing to relocate) is always local — no target resolution.
    local_layer = _ENGINES.get(execution_path)
    if (isinstance(local_layer, ExecutionLayer)
            and not local_layer.capabilities.runtime.supports_remote_execution):
        return local_layer
    if placement_built is not None and placement_built.kind == placement.KIND_COMPUTE:
        return _get_remote_layer()

    # Resolve effective target unless the caller already resolved one while
    # building its config. Returns (target, fallback_reason). Skipping
    # re-resolution avoids re-running user/role logic the caller may not
    # replicate; the isolation guards below run on the passed target either way.
    from services.remote import remote_status
    from storage import remote_store
    if not execution_target:
        execution_target, _reason = remote_status.resolve_execution_target(
            agent_name, user_sub, role,
        )
    if placement.is_offline_sentinel(execution_target):
        # Resolver decided the intended remote target is unreachable and no
        # fallback is allowed. Hard-fail here so we never silently run on the
        # wrong machine (different MCPs, different filesystem, etc.). Callers
        # that legitimately need to operate on offline agents (e.g. shutdown
        # close_session) wrap this in try/except.
        offline_machine_id = placement.offline_machine_of(execution_target)
        from services.remote import machine_away
        raise RuntimeError(machine_away.away_sentence(
            offline_machine_id, "",
            f"Agent '{agent_name}' targets remote machine "
            f"{offline_machine_id[:8]} which is offline. Bring the satellite "
            f"back online or change the agent's execution target."))
    if not placement.is_local(execution_target):
        # Per-user satellite isolation: agent-scope sessions
        # (scheduled tasks, phone, triggers — no user_sub) must never run
        # on user-paired machines, which have only ONE user's data and no
        # service-account credential surface. Refusal here fails the
        # session start with a clear error instead of silently routing to
        # a machine that can't serve the session.
        machine = remote_store.get_remote_machine(execution_target)
        if placement.machine_is_compute(machine):
            # A compute machine's id is never a stored place: it reaches
            # here only as a live record's target (a terminal the machine
            # holds), whose layer is the remote one.
            return _get_remote_layer()
        if machine and not placement.machine_is_admin_paired(machine):
            # `not user_sub` (NOT `is None`) — `pick_account` treats both None
            # AND "" as service-scope, so a service-account session with an empty
            # user_sub must also be refused here, else its service-account
            # credentials (a GitHub token, an MCP bearer, …) would land on a user-owned
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
                    "An admin turned off personal computers for agents. Ask an admin "
                    "to turn them back on, or pick another machine for this agent."
                )
        return _get_remote_layer()

    if execution_path not in _ENGINES:
        # FAIL CLOSED. This used to fall back to the CLI layer, which meant a
        # typo'd or retired engine id ran the agent on Claude Code — the wrong
        # binary, the wrong credentials, the wrong sandbox, silently. An id
        # that no layer claims is a configuration error, and the caller must
        # see it.
        raise UnknownExecutionPath(
            f"Agent '{agent_name}' is configured with execution_path "
            f"'{execution_path}', which no AI engine is registered for. "
            f"Known engines: {', '.join(sorted(_ENGINES))}."
        )
    # The platform's place with no compute placement: the platform starts no
    # session of a process engine itself, so there is no layer to answer.
    raise placement.NoComputeMachine()


def resolve_execution_path(agent_name: str, execution_path: str = "") -> str:
    """Resolve the actual execution_path for an agent (ignoring remote routing).

    Returns one of the registered engine ids — never 'remote'. Used by callers
    that need the path for config building or DB storage.
    """
    if execution_path:
        return execution_path
    agent = agent_store.get_agent(agent_name)
    return (agent or {}).get("execution_path") or DEFAULT_EXECUTION_PATH


def get_layer_by_path(execution_path: str) -> Engine:
    """The engine for a given execution_path string: its facts, never a
    session of it (who holds a session is ``find_layer_for_session``).

    Raises ``UnknownExecutionPath`` for an unregistered id — see
    get_execution_layer.
    """
    layer = _ENGINES.get(execution_path)
    if not layer:
        raise UnknownExecutionPath(
            f"No AI engine is registered for execution_path "
            f"'{execution_path}'. Known engines: {', '.join(sorted(_ENGINES))}."
        )
    return layer


def get_all_layers() -> dict[str, Engine]:
    """Every registered engine, by id."""
    return dict(_ENGINES)


def get_layer_capabilities(execution_path: str) -> LayerCapabilities | None:
    """Return the LayerCapabilities for a given execution_path, or None.

    The tolerant form, for pure QUERIES that must not raise (placement
    resolution, the prompt roster, a machine card) — an unknown id reads as
    "supports nothing". Builders and spawn paths use ``capabilities_for_path``
    below, which fails closed like ``get_execution_layer``.
    """
    layer = _ENGINES.get(execution_path)
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
        for path, layer in _ENGINES.items()
    }
