"""Headless MCP tool execution for pinned app ``mcp_tool`` actions.

A declared, user-approved app button invokes ONE MCP tool directly — no agent
session, no LLM turn, no token spend. Execution reuses the Direct-LLM MCP
client (``core/layers/direct/mcp.py``) under a SYNTHETIC session identity:

* **Identity is the task posture, verbatim** (``resolve_task_identity``):
  a shared app runs agent-scoped (service credentials, role manager — admin
  only for admin-only agents, exactly what the task scheduler grants); a
  personal app runs as its OWNER (their per-agent role + per-user MCP
  credentials). Never wider than an unattended task of the same scope.
* **A presser's own values never reach past the presser** (``press_reach``):
  a press below the identity runs in a session of its own, the agent scope
  at the presser's role or no folder of the agent at all, with the
  identity's accounts and tools.
* **Sandbox parity is fail-closed**: stdio MCPs get the same per-MCP bwrap
  wrapping a Direct-LLM session builds (mounts + netns from
  ``resolve_sandbox_config``). If the sandbox build fails, the click is
  DENIED — never a fallback to an unwrapped subprocess.
* **One pooled ``AgentMCPManager`` per (agent, scope owner, MCP set)** keeps
  the MCP subprocesses warm across clicks (first click pays the spawn
  latency). The set is the MCPs the app's manifest names, not the agent's
  full roster: a connector-heavy agent's dashboard starts one MCP, not
  fifteen. An app whose set is covered by a warm sibling's reuses that
  manager; a missing tool rebuilds with the union. A personal app's manager
  carries the owner's credentials and its pool key carries the owner's sub,
  so it can never serve another user — the route layer already restricts
  personal apps to their owner (asserted here too).
* **Keep-warm**: the host page asks for the manager of an app it just
  opened (``warm``); the entry then survives idle for ``KEEP_WARM_S`` and
  LRU eviction skips it while pinned, unless every entry is pinned.
* **Full-session teardown on reap**: OAuth writeback → close (terminates the
  bwrap children) → ``cleanup_session_permission_state`` (drops the security
  context + brokered secrets), so the synthetic session leaves nothing
  behind. Idle timeout is the platform ``session_idle_timeout`` knob.

The route layer (``api/apps/apps.py``) owns every AUTH gate — approval sig,
approver re-check, MCP-still-assigned, args-schema validation, rate limits.
This module owns identity synthesis and execution only.
"""

import asyncio
import dataclasses
import hashlib
import json
import logging
import time
import uuid

import psycopg

import config
from core import placement
from core.background import spawn
from core.layers.direct.mcp import ENGINE_ID, AgentMCPManager
from core.session import session_kind
from services.mcp import mcp_installer
from storage import db_apps

logger = logging.getLogger("otodock.apps")

RESULT_MAX_CHARS = 32 * 1024
POOL_MAX = 8            # LRU-evicted; each entry is a set of MCP subprocesses
START_CONCURRENCY = 2   # simultaneous manager cold-starts (subprocess spawns)
START_TIMEOUT_S = 90    # backstop over mgr.start() — a wedged MCP handshake
                        # must surface as a click error, not a hung request
CALL_CONCURRENCY = 4    # tool calls in flight on one manager (a batch fans
                        # out this wide; the rest queue on the entry)
KEEP_WARM_S = 15 * 60   # idle grace + LRU pin after a warm request

# A missing tool triggers ONE rebuild ("self-heal": the MCP may have been
# re-enabled or crashed since the manager warmed) — but a tool whose MCP
# flakes on EVERY start must not turn each click into a full manager
# teardown+rebuild. Found live 2026-07-10: uptime-kuma failing "Connection
# closed" on startup put the system-admin manager in a rebuild-per-refresh
# loop, ~15s each (unifi's connect retries dominate the start). Between
# rebuilds the missing tool fails fast with the same click error.
_SELF_HEAL_COOLDOWN_S = 120

_SWEEP_INTERVAL_S = 60

# A press's reach (``press_reach``): ``("", "")`` the identity itself,
# ``(PRESS_MEMBER, role)`` the agent scope at that role, ``(PRESS_NONE,
# "viewer")`` no folder of the agent.
Reach = tuple[str, str]
FULL: Reach = ("", "")


class _Entry:
    __slots__ = ("manager", "session_id", "scope_key", "mcps", "reach", "last_used",
                 "_busy", "idle", "sem", "idle_s", "pinned_until")

    def __init__(self, manager: AgentMCPManager, session_id: str, scope_key: str,
                 mcps: frozenset[str], reach: Reach = FULL):
        self.manager = manager
        self.session_id = session_id
        self.scope_key = scope_key
        self.mcps = mcps
        self.reach = reach
        self.last_used = time.monotonic()
        self._busy = 0
        self.idle = asyncio.Event()        # set while no call runs on the entry
        self.idle.set()
        self.sem = asyncio.Semaphore(CALL_CONCURRENCY)
        self.idle_s: float | None = None   # None → the platform idle timeout
        self.pinned_until = 0.0            # monotonic; LRU skips while pinned

    @property
    def busy(self) -> int:
        """The calls running on the entry's manager."""
        return self._busy

    @busy.setter
    def busy(self, count: int) -> None:
        self._busy = count
        if count:
            self.idle.clear()
        else:
            self.idle.set()


PoolKey = tuple[str, str, str, Reach]

# (agent, scope_key, mcps_key, reach) → entry. scope_key is "" for shared
# apps, the OWNER's sub for personal apps — the credential boundary lives in
# this key; mcps_key is the sorted MCP set the manager was built with; reach
# keeps a confined press off a manager of a wider one.
_pool: dict[PoolKey, _Entry] = {}
_key_locks: dict[PoolKey, asyncio.Lock] = {}
_start_sem = asyncio.Semaphore(START_CONCURRENCY)
_inflight: set[tuple[str, str]] = set()  # (app_id, action_id|args-fp) — one at a time
_selfheal_at: dict[PoolKey, float] = {}  # last rebuild-for-missing-tool
_sweeper: asyncio.Task | None = None


def _scope_key(row: dict) -> str:
    return (row.get("owner_sub") or "") if row.get("username") else ""


def _mcps_key(mcps: frozenset[str]) -> str:
    return ",".join(sorted(mcps))


def _pool_key(agent: str, row: dict, mcps: frozenset[str], reach: Reach = FULL) -> PoolKey:
    return (agent, _scope_key(row), _mcps_key(mcps), reach)


def _entry_key(agent: str, entry: _Entry) -> PoolKey:
    return (agent, entry.scope_key, _mcps_key(entry.mcps), entry.reach)


def chooses(action: dict, validated: dict) -> bool:
    """Whether the page chose something the approver did not list: a string
    with no ``enum`` (a declared file field included: the platform cannot
    know what the tool does with it) or a list. A number, a boolean or an
    enum's member names nothing new."""
    props = (action.get("args_schema") or {}).get("properties") or {}
    for name in validated:
        spec = props.get(name) or {}
        if spec.get("type") == "array" or (spec.get("type") == "string" and "enum" not in spec):
            return True
    return False


def press_reach(row: dict, user, *, actor: str = "", handler: str = "",
                chose: bool) -> Reach:
    """As whom a press reads and writes:
    the button's identity, or the presser's own reach when that is below it.

    Full: a handler's or step's press (the app's own code chose the values);
    the owner of a personal app; on a shared app a press that chooses
    nothing (the approver's delegation), a platform admin, and on an agent
    that is not admin-only a member at the editor tier or above (the tier
    whose agent-scope session sees the agent's engine state). Any other
    member: the agent scope at their role, on a personal app whatever they
    chose (its identity holds one person's own folder). Anyone with no
    place on the agent (a share admits to the app, not the agent), a
    link's visitor, a bearer of no member: no folder at all. Decided from
    the platform's records, never from ``manifest.caller_role``, which
    gives a non-member their share's role."""
    from auth import roles
    from auth.path_policy import PRESS_MEMBER, PRESS_NONE
    if handler:
        return FULL
    agent = row.get("agent") or ""
    personal = bool(row.get("username"))
    if not personal and not chose:
        return FULL
    none: Reach = (PRESS_NONE, roles.VIEWER)
    if user is None or actor or getattr(user, "is_external", False):
        return none
    if personal and (row.get("owner_sub") or "") == user.sub and not getattr(user, "is_api_key", False):
        return FULL
    if not user.can_access_agent(agent):
        return none
    if getattr(user, "is_api_key", False):
        return (PRESS_MEMBER, roles.VIEWER)
    if not personal and user.is_admin:
        return FULL
    role = user.acting_role(agent) or roles.VIEWER
    if not personal and roles.can_edit(role) and not _admin_only(agent):
        return FULL
    if roles.is_admin(role):
        role = roles.MANAGER
    return (PRESS_MEMBER, role)


def _admin_only(agent: str) -> bool:
    from storage.agents import agent_store
    return bool(agent_store.is_admin_only(agent))


def manifest_mcps(row: dict) -> frozenset[str]:
    """The MCP keys the row's manifest names in ``mcp_tool`` actions — the
    set a manager for this app is built from. Parsed here rather than via
    the API-layer manifest helpers so this service stays importable on its
    own."""
    try:
        actions = json.loads(row.get("actions") or "[]")
    except (TypeError, ValueError):
        return frozenset()
    if not isinstance(actions, list):
        return frozenset()
    return frozenset(
        str(a.get("mcp") or "") for a in actions
        if isinstance(a, dict) and a.get("type") == "mcp_tool" and a.get("mcp")
    )


def _build_session_parts(agent: str, row: dict, mcps: frozenset[str], session_id: str = "",
                         *, phone: bool = False, reach: Reach = FULL):
    """Blocking build (runs in a thread): MCP config for the app's identity,
    sandbox builder, and the SecurityContext to register. Mirrors
    ``task_config_builder`` (identity/visibility/path_env) and the Direct-LLM
    layer's ``start_session`` (sandbox mounts + stdio dir binds), restricted
    to the manifests whose mcpServers key is in ``mcps``: mounts, stdio dir
    binds and secret bundles cover only what this manager starts. ``phone``
    builds with an outside session's contexts instead of a task's (the tool
    listing: a server a manifest keeps out of tasks is still listed).
    ``reach`` narrows a press below the identity (``press_reach``): the
    identity's accounts, tools and placement, and the files, the sandbox and
    the routes of the agent scope at the presser's role, or of no folder."""
    from auth import roles
    from auth.path_policy import PRESS_NONE, SecurityContext
    from core.config.task_config_builder import resolve_task_identity
    from core.layers.cli.config_dir import ensure_persistent_claude_dir
    from core.sandbox.sandbox import (
        SandboxBuilder,
        SandboxMount,
        resolve_sandbox_config,
    )
    from core.session.session_manager import compute_placement_for
    from core.session.visibility import resolve_visibility
    from services import path_roles
    from services.mcp import mcp_registry
    from storage.agents import agent_store

    scope = "user" if row.get("username") else "agent"
    identity = resolve_task_identity(agent, scope, row.get("owner_sub") or None)
    vis = resolve_visibility(
        agent,
        username=identity.username,
        user_role=identity.role,
        user_sub=identity.creds_user_sub or "",
        scope_override=identity.scope,
    )
    # The files' identity: the button's own, or a press below it at the
    # lower of the two roles, never admin (the admin shortcut never runs).
    file_role, file_vis = identity.role, vis
    if reach != FULL:
        kind, presser_role = reach
        file_role = min((presser_role, identity.role), key=roles.rank)
        if kind == PRESS_NONE or roles.is_admin(file_role):
            file_role = roles.VIEWER if kind == PRESS_NONE else roles.MANAGER
        file_vis = resolve_visibility(agent, username="", user_role=file_role,
                                      user_sub="", scope_override="agent")

    # Task-context build (a button IS unattended machine-fired execution):
    # task_mode applies the manifests' task exclusions; identity.creds_user_sub
    # resolves per-user credentials for personal apps and the service accounts
    # for shared ones. Device and satellite MCPs are rejected at pin time and
    # never reach this path. Its stdio tools run where a Direct LLM session's
    # do: on the compute machine that serves. With none the button, or the
    # listing, is refused with the sentence: the platform starts no tool.
    where = compute_placement_for(ENGINE_ID, agent=agent)
    if where is None:
        raise placement.NoComputeMachine()
    # The agent's tool limits for the run's identity (``tool_limits``): a
    # personal app's button behind its owner (in every mode,
    # ``TaskIdentity.person``), a shared app's behind none.
    from services.mcp import tool_limits
    limits = tool_limits.load(agent, tool_limits.governs_of(external=False, person=identity.person))
    mcp_config, credential_env, _excl, secret_bundles, _bash = (
        mcp_registry.build_session_mcp_config(
            agent, identity.creds_user_sub,
            session_id=session_id,
            task_mode=not phone,
            phone_mode=phone,
            task_scope=identity.scope,
            username=identity.username,
            user_role=identity.role,
            task_owner=identity.creds_user_sub or "",
            task_username=identity.username if identity.creds_user_sub else "",
            placement=where,
            limits=limits or None,
        )
    )
    credential_env = dict(credential_env or {})
    secret_bundles = {k: v for k, v in (secret_bundles or {}).items() if k in mcps}

    # Manifest-declared path_env values (same loop as task_config_builder).
    assigned = [
        m for m in (mcp_registry.get_agent_mcps(agent, placement=where) or [])
        if (getattr(m, "server_name", "") or m.name) in mcps
    ]
    for manifest in assigned:
        if not manifest.path_env:
            continue
        for env_var, decl in manifest.path_env.items():
            try:
                credential_env[env_var] = path_roles.resolve_path_env_entry(
                    decl, username=file_vis.mount_username, user_role=file_role,
                )
            except ValueError as e:
                logger.warning(
                    "app exec: path_env injection failed for %s.%s: %s",
                    manifest.name, env_var, e,
                )

    # An app button runs its MCPs in-process, the way the Direct LLM layer
    # does: it wants the ``.claude`` tree (the MCP config home the sandbox
    # mounts), not an engine — so it asks for that dir by name.
    host_claude_dir = ensure_persistent_claude_dir(
        agent,
        username=file_vis.mount_username, scope=file_vis.mount_scope,
    )

    # Per-MCP bwrap for stdio subprocesses — the same mounts/binds the
    # Direct-LLM layer builds. NO None fallback: a raise here denies the click.
    mcp_mounts: list[SandboxMount] = []
    for manifest in assigned:
        for m in getattr(manifest, "sandbox_mounts", []):
            host = m.host.replace("${mcp_dir}", str(manifest.mcp_dir))
            mcp_mounts.append(SandboxMount(host=host, sandbox=m.sandbox, mode=m.mode))
    stdio_dirs = [
        str(manifest.mcp_dir) for manifest in assigned
        if manifest.server.transport == "stdio"
    ]
    if stdio_dirs:
        # Every interpreter folder: the render skips one its host lacks.
        stdio_dirs.extend(str(d) for d in mcp_installer.interpreter_dirs(config.MCPS_DIR.resolve()))

    is_admin_agent = agent_store.is_admin_only(agent)
    sandbox_cfg = resolve_sandbox_config(
        role=file_role,
        username=file_vis.mount_username,
        agent_name=agent,
        is_admin_agent=is_admin_agent,
        host_claude_dir=host_claude_dir,
        user_sub=identity.creds_user_sub or "",
        mcp_sandbox_mounts=mcp_mounts,
        config_visible=file_vis.config_visible if reach == FULL else False,
        mount_shared=file_vis.mount_shared,
        mcp_dir_binds=stdio_dirs,
        app_press=True,
    )
    if reach[0] == PRESS_NONE:
        # No folder of the agent: the table an app server gets, empty.
        sandbox_cfg = dataclasses.replace(sandbox_cfg, app_mounts=[], app_cwd="/tmp")
    sandbox_builder = SandboxBuilder(sandbox_cfg)

    ctx = SecurityContext(
        role=file_role,
        # REAL owner for attribution ("" shared); a press below the identity
        # has no person's folder, so no username.
        username=identity.username if reach == FULL else "",
        agent=agent,
        is_admin_agent=is_admin_agent,
        session_scope=file_vis.mount_scope,
        # The self-config tool is held to nothing here: a press runs with no
        # hook and no card, and reaches the manager directly.
        tool_limits=tool_limits.merged(
            tool_limits.grants(limits, [m for m in assigned if m.name not in (_excl or {})]),
            ((tool_limits.AGENT_CONFIG_MCP, ()),)),
        config_visible=vis.config_visible if reach == FULL else False,
        available_scopes=vis.available_scopes,
        # PINNED False: app action clicks are not scheduled fires —
        # the knowledge_rw opt-in never applies here, whoever owns the app.
        knowledge_rw=False,
        # A tool process on a compute machine reaches the platform through
        # that machine's link, which admits the session the record names.
        placement=where,
        press_reach=reach[0],
        app_press=True,
    )
    return mcp_config, secret_bundles, credential_env, sandbox_builder, ctx


async def open_manager(agent: str, row: dict, mcps: frozenset[str], *,
                       prefix: str = "appx", phone: bool = False, reach: Reach = FULL) -> _Entry:
    """One manager started for the agent's identity (``row``: an app's,
    ``{}`` for the agent's own scope) with the named servers alone, under a
    synthetic session that is registered before the start and closed by
    ``close_manager``. The pool below and the tool listing
    (``services/mcp/tool_listing``, with ``phone``) both start their
    servers here."""
    session_id = f"{prefix}-{uuid.uuid4().hex[:12]}"
    mcp_config, bundles, credential_env, builder, ctx = await asyncio.to_thread(
        _build_session_parts, agent, row, mcps, session_id, phone=phone, reach=reach,
    )
    # Register the synthetic session BEFORE start: MCP subprocesses hold a
    # session JWT (minted in build_session_env) and their hook callbacks
    # (resolve-path, display, …) resolve through this context.
    from core.session.session_state import (
        _record_session_use,
        mark_headless_live,
        set_session_security,
    )
    set_session_security(session_id, ctx)
    mark_headless_live(session_id)
    _record_session_use(session_id, client_type=session_kind.APP.name, agent=agent)

    built = placement.compute_of(ctx.placement)
    mgr = AgentMCPManager(
        agent,
        credential_env=credential_env,
        session_id=session_id,
        sandbox_builder=builder,
        prebuilt_config=(mcp_config, bundles),
        included_mcps=set(mcps),
        machine_id=str(built.machine_id) if built is not None else "",
    )
    entry = _Entry(mgr, session_id, "", mcps, reach)
    try:
        # The credential exit: the compute machine's own, as a Direct LLM
        # session's tools get it, so a placeholder the build minted has the
        # exit that fills it (the door on each tool's open, the values
        # pushed before the first tool). Where the one machine rule refuses
        # the machine, the build left the tools that need a key out and this
        # attaches nothing.
        from core.credentials import credential_exit
        if built is not None:
            await credential_exit.attach_sandbox(
                session_id, builder, {}, builder.cfg.host_claude_dir,
                sandbox_config_dir=builder.get_env_overrides().get("CLAUDE_CONFIG_DIR", "/workspace/.claude"),
                machine=built)
        async with _start_sem:
            await asyncio.wait_for(mgr.start(), timeout=START_TIMEOUT_S)
    except BaseException:
        # A start the caller cancels (the listing's own bound) closes too:
        # its session, its bundles and its exit never outlive it.
        await asyncio.shield(close_manager(entry))
        raise
    return entry


async def _create_entry(agent: str, row: dict, scope_key: str,
                        mcps: frozenset[str], reach: Reach = FULL) -> _Entry:
    entry = await open_manager(agent, row, mcps, reach=reach)
    entry.scope_key = scope_key
    logger.info(
        "Headless MCP manager started: agent=%s, "
        "scope=%s, reach=%s, "
        "mcps=%s, session=%s",
        agent, 'personal' if scope_key else 'shared', reach[0] or "full",
        _mcps_key(mcps), entry.session_id
    )
    return entry


async def close_manager(entry: _Entry) -> None:
    """Full synthetic-session teardown: close (terminates bwrap children)
    → permission-state cleanup (security ctx + brokered secrets). Each step
    best-effort so one failure never strands the rest."""
    from core.session.session_state import (
        clear_headless_live,
        forget_session,
        mark_closing,
    )
    mark_closing(entry.session_id)
    clear_headless_live(entry.session_id)
    try:
        await entry.manager.close()
    except Exception:
        logger.exception("app exec: manager close failed")
    await forget_session(entry.session_id)


def _key_lock(key: PoolKey) -> asyncio.Lock:
    lock = _key_locks.get(key)
    if lock is None:
        lock = _key_locks[key] = asyncio.Lock()
    return lock


def _covering_entry(agent: str, scope_key: str, mcps: frozenset[str],
                    reach: Reach = FULL) -> _Entry | None:
    """A warm manager of the same identity and the same reach whose MCP set
    covers ``mcps`` — the exact set first, else the smallest superset."""
    exact = _pool.get((agent, scope_key, _mcps_key(mcps), reach))
    if exact is not None:
        return exact
    candidates = [
        e for k, e in _pool.items()
        if k[0] == agent and k[1] == scope_key and k[3] == reach and mcps <= e.mcps
    ]
    return min(candidates, key=lambda e: len(e.mcps)) if candidates else None


async def _get_entry(agent: str, row: dict, mcps: frozenset[str],
                     reach: Reach = FULL) -> tuple[_Entry, bool]:
    """Warm entry covering ``mcps`` at ``reach`` or a fresh build of exactly
    that set; the second element says WHICH (a fresh build that lacks the
    wanted tool must not be torn down and rebuilt — the MCP just failed to
    start)."""
    key = _pool_key(agent, row, mcps, reach)
    async with _key_lock(key):
        entry = _covering_entry(agent, key[1], mcps, reach)
        if entry:
            entry.last_used = time.monotonic()
            return entry, False
        while len(_pool) >= POOL_MAX:
            # Prefer idle, unpinned victims: disposing a manager mid-call
            # kills its bwrap children under the running tool, and a pinned
            # entry is a dashboard someone is looking at. Only when EVERY
            # entry is busy does the cap win over the in-flight call.
            now = time.monotonic()
            idle = [k for k in _pool if not _pool[k].busy]
            unpinned = [k for k in idle if _pool[k].pinned_until <= now]
            oldest_key = min(unpinned or idle or _pool,
                             key=lambda k: _pool[k].last_used)
            oldest = _pool.pop(oldest_key)
            logger.info("Evicting headless MCP manager (pool cap): %s", oldest_key)
            # Behind the press: the button never waits on the old close.
            spawn(close_manager(oldest), name="evict", owner="headless_exec")
        entry = await _create_entry(agent, row, key[1], mcps, reach)
        _pool[key] = entry
        _ensure_sweeper()
        return entry, True


async def drop_idle(agent: str) -> int:
    """Drop the agent's idle managers, so a press after the agent's accounts
    or limits changed builds anew; a busy one is left to its press and the
    sweep. The count dropped."""
    keys = [k for k, e in _pool.items() if k[0] == agent and not e.busy]
    for key in keys:
        await _drop_entry(key)
    return len(keys)


async def _drop_entry(key: PoolKey) -> None:
    async with _key_lock(key):
        entry = _pool.pop(key, None)
    if entry:
        if entry.busy:
            logger.warning("app exec: dropping a busy headless manager (self-heal)")
        await close_manager(entry)


async def _wait_idle(entry: _Entry) -> None:
    """Block until no call runs on the entry (bounded by the longest call a
    manager allows): a self-heal rebuild must never dispose a manager under
    a sibling's running tool."""
    deadline = time.monotonic() + entry.manager.tool_timeout + 30
    # A call can start between the last one's end and this wake: the count is
    # read again after every wake.
    while entry.busy:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return
        try:
            await asyncio.wait_for(entry.idle.wait(), remaining)
        except TimeoutError:
            return


def _ensure_sweeper() -> None:
    global _sweeper
    if _sweeper is None or _sweeper.done():
        _sweeper = asyncio.get_running_loop().create_task(_sweep_loop())


async def _sweep_once() -> None:
    """Reap managers idle past their grace (the platform idle timeout, or
    the keep-warm grace a warm request set)."""
    from core.session import session_state as _state
    platform_timeout = await _state.cached_idle_timeout()
    now = time.monotonic()
    for key in list(_pool):
        entry = _pool.get(key)
        if not entry or entry.busy:
            continue
        timeout = entry.idle_s if entry.idle_s else platform_timeout
        if now - max(entry.last_used, entry.manager.last_activity) <= timeout:
            continue
        async with _key_lock(key):
            current = _pool.get(key)
            if current is not entry or entry.busy:
                continue
            _pool.pop(key, None)
        logger.info("Reaping idle headless MCP manager: %s", key)
        await close_manager(entry)


async def _sweep_loop() -> None:
    """Periodic reaper. Exits when the pool empties (restarted lazily on the
    next create)."""
    while True:
        await asyncio.sleep(_SWEEP_INTERVAL_S)
        if not _pool:
            return
        await _sweep_once()


async def close_all() -> None:
    """Dispose every pooled manager (tests + shutdown)."""
    for key in list(_pool):
        await _drop_entry(key)
    _selfheal_at.clear()


async def warm(row: dict, reach: Reach = FULL) -> bool:
    """Build (or touch) the manager for the row's manifest set at ``reach``
    (the class the opener's own presses take) and keep it warm: idle grace
    and LRU pin for ``KEEP_WARM_S``. False when there is nothing to build
    (no ``mcp_tool`` action) or the identity fails closed. The route layer
    gates access and approval; the build is the same one a click would pay,
    so the click after this one is instant."""
    agent = row.get("agent") or ""
    mcps = manifest_mcps(row)
    if not mcps:
        return False
    if row.get("username"):
        ok = await asyncio.to_thread(db_apps.owner_holds_agent, agent, row.get("owner_sub") or "")
        if not ok:
            return False
    entry, _ = await _get_entry(agent, row, mcps, reach)
    entry.idle_s = KEEP_WARM_S
    entry.pinned_until = time.monotonic() + KEEP_WARM_S
    return True


def timed_out_sentence(seconds: int) -> str:
    """What a page is told when the platform stopped waiting for its tool."""
    return f"The tool did not finish within {seconds} seconds. It may still be working."


def warm_reaches(row: dict, user) -> list[Reach]:
    """The classes an opener's presses take (``press_reach``): the one a
    press that chooses nothing takes, and the one a press that chooses
    takes when the app has a button that lets the page choose and whose
    floor the opener meets. Blocking (the floor reads the store): call it in
    a thread."""
    from services.apps import manifest as _mf
    plain = press_reach(row, user, chose=False)
    out = [plain]
    chosen = press_reach(row, user, chose=True)
    if chosen != plain:
        try:
            actions = json.loads(row.get("actions") or "[]")
        except (TypeError, ValueError):
            actions = []
        choosing = [a for a in actions if isinstance(a, dict) and a.get("type") == "mcp_tool"
                    and any(chooses(a, {k: None}) for k in
                            ((a.get("args_schema") or {}).get("properties") or {}))]
        if choosing:
            role = _mf.caller_role(row, user)
            if any(_mf.meets_floor(a, role) for a in choosing):
                out.append(chosen)
    return out


async def execute_app_tool(row: dict, action: dict, merged_args: dict, *, reach: Reach) -> dict:
    """Execute one declared ``mcp_tool`` action with fully-validated, merged
    args, at the press's ``reach`` (``press_reach``; required, so a new
    caller decides it). Returns ``{"status": "done", "result": <text>}``
    when the tool ran (tool-level errors come back as the tool's own error
    text — the page renders it), or ``{"status": "error", "reason": …}`` for
    infrastructure failures and a call the platform stopped waiting for.
    All AUTH gates ran in the route layer before this."""
    agent = row.get("agent") or ""
    app_id = row.get("id") or ""
    action_id = action.get("id") or ""
    mcps = manifest_mcps(row) | {str(action.get("mcp") or "")}
    # Args-aware flight key: one declared action often serves many widgets
    # (a parameterized ``toggle`` with the entity in args) — different args
    # are independent calls; only an IDENTICAL repeat is "already running".
    args_fp = hashlib.sha256(json.dumps(
        merged_args, sort_keys=True, separators=(",", ":"),
    ).encode("utf-8")).hexdigest()[:16]
    flight = (app_id, f"{action_id}|{args_fp}")
    if flight in _inflight:
        return {"status": "error", "reason": "This action is already running"}
    _inflight.add(flight)
    try:
        if row.get("username"):
            ok = await asyncio.to_thread(
                db_apps.owner_holds_agent, agent, row.get("owner_sub") or "",
            )
            if not ok:
                return {"status": "error",
                        "reason": "Approval stale — the app owner no longer has access to this agent"}

        namespaced = f"mcp__{action.get('mcp')}__{action.get('tool')}"
        try:
            entry, created = await _get_entry(agent, row, mcps, reach)
            # The agent's tool limits hold on a button's call as on a
            # session's (the run's context, registered at the build).
            from core.session.session_state import get_session_security
            from services.mcp import tool_limits
            limited = tool_limits.refusal(
                getattr(get_session_security(entry.session_id), "tool_limits", ()) or (),
                str(action.get("mcp") or ""), str(action.get("tool") or ""))
            if limited:
                return {"status": "error", "reason": limited}
            if not entry.manager.has_tool(namespaced) and not created:
                # Self-heal: the MCP may have been (re-)enabled or crashed
                # since this WARM manager was built — rebuild with the union
                # of its set and this app's, and retry. Never on a fresh
                # build (the MCP just failed to start), at most once per
                # cooldown per key (a flaky MCP must not turn every click
                # into a full teardown+rebuild), and only once the entry is
                # idle: a batch's sibling calls may still be running on it.
                key = _entry_key(agent, entry)
                now = time.monotonic()
                if now - _selfheal_at.get(key, 0.0) >= _SELF_HEAL_COOLDOWN_S:
                    _selfheal_at[key] = now
                    logger.info(
                        "app exec: self-heal rebuild for %s "
                        "(missing %s)", key, namespaced
                    )
                    await _wait_idle(entry)
                    if _pool.get(key) is entry:
                        await _drop_entry(key)
                    entry, _ = await _get_entry(agent, row, entry.mcps | mcps, reach)
        except placement.NoComputeMachine as e:
            # Nothing can run the tool now: the one sentence, no stack.
            logger.info("app exec: no compute machine serves %s now", agent)
            return {"status": "error", "reason": str(e)}
        except Exception as e:
            logger.exception("app exec: manager start failed for %s", agent)
            return {"status": "error", "reason": f"Could not start the tool's MCP: {e}"}
        if not entry.manager.has_tool(namespaced):
            logger.warning(
                "app exec: tool unavailable: app=%s, "
                "tool=%s (its MCP likely failed to start — "
                "see mcp-manager errors above)", row.get('slug'), namespaced
            )
            return {"status": "error",
                    "reason": f"Tool '{action.get('tool')}' is not available on MCP '{action.get('mcp')}'"}
        # scope_key is derived from the row on BOTH sides; this assert is the
        # credential-boundary belt for future refactors.
        assert entry.scope_key == _scope_key(row), "headless pool scope mismatch"
        assert entry.reach == reach, "headless pool reach mismatch"

        for attempt in (1, 2):
            entry.busy += 1
            entry.last_used = time.monotonic()
            try:
                # execute_tools bounds the CALL (the manager's tool_timeout —
                # 60 s for app actions — surfaced as error text); this outer
                # margin only covers a wedged dispatch layer — the click must
                # ALWAYS get a terminal result.
                async with entry.sem:
                    results = await asyncio.wait_for(
                        entry.manager.execute_tools(
                            [{"id": "app-action", "name": namespaced, "input": merged_args}],
                        ),
                        timeout=entry.manager.tool_timeout + 30,
                    )
            except asyncio.TimeoutError:
                logger.error("app exec: dispatch timed out for %s", namespaced)
                return {"status": "error", "reason": timed_out_sentence(entry.manager.tool_timeout + 30)}
            finally:
                entry.busy -= 1
            # A WARM manager whose server died under the call (the server
            # evicted the idle session while the manager sat in the pool):
            # one fresh manager and one more press, so the hourly wake
            # after a quiet stretch is not the one that fails.
            if attempt == 1 and not created and not entry.manager.has_tool(namespaced):
                key = _entry_key(agent, entry)
                logger.info("app exec: %s died under the call, rebuilding %s once", namespaced, key)
                try:
                    await _wait_idle(entry)
                    if _pool.get(key) is entry:
                        await _drop_entry(key)
                    entry, created = await _get_entry(agent, row, mcps, reach)
                except Exception:
                    logger.exception("app exec: rebuild after a dead call failed for %s", agent)
                    break
                if entry.manager.has_tool(namespaced):
                    continue
            break
        if results and results[0].get("timed_out"):
            # The platform stopped waiting: a failure the page can show, never
            # a success whose text is an error.
            return {"status": "error", "reason": timed_out_sentence(entry.manager.tool_timeout)}
        text = (results[0].get("content") if results else "") or "(empty result)"
        if len(text) > RESULT_MAX_CHARS:
            text = text[:RESULT_MAX_CHARS] + "\n… (result truncated)"
        logger.info(
            "App tool fired: app=%s, action=%s, "
            "tool=%s, scope=%s",
            row.get('slug'), action_id, namespaced, 'personal' if row.get('username') else 'shared'
        )
        return {"status": "done", "result": text}
    finally:
        _inflight.discard(flight)


# ---------------------------------------------------------------------------
# The account a call runs with (APPS.md "Account arguments")
# ---------------------------------------------------------------------------

ACCOUNT_EMAIL_TOKEN = "${account.email}"


def runs_as(row: dict) -> tuple[str, str]:
    """``(user_sub, word)``: the identity an app's tool calls run with — a
    personal app runs as its owner (``""`` sub means the agent's service
    account, for a shared app). The word names it for a card: "the owner"
    or "the agent"; a caller substitutes "you" for the owner themself."""
    if row.get("username"):
        return row.get("owner_sub") or "", "the owner"
    return "", "the agent"


def connected_account(mcp_name: str, agent: str, user_sub: str) -> str:
    """The email (or label) of the account ``mcp_name`` calls run with for
    ``user_sub`` on ``agent`` — the viewer's bound or default account for a
    user, the agent's service binding for ``""`` and on an agent whose
    people use its accounts (``account_context.reader_scope``) — or ``""``
    when none is connected. The same lookup the token map makes for a chat's
    ``${account.email}`` (services/mcp/dynamic_context.py)."""
    from services.mcp import account_context
    from services.oauth import credential_resolver
    from storage.identity import credential_store
    try:
        _scope, person = account_context.reader_scope(agent, user_sub)
        ref = credential_resolver.pick_account(mcp_name, agent, user_sub=person)
    except Exception:
        logger.exception("app exec: account lookup failed for %s on %s", mcp_name, agent)
        return ""
    if ref is None:
        return ""
    try:
        accounts = credential_store.list_user_accounts(ref.owner_sub, mcp_name) or []
    except psycopg.Error:
        accounts = []
    match = next((a for a in accounts if a.get("account_label") == ref.label), None)
    return str((match or {}).get("display_email") or ref.label)


def fill_account_args(row: dict, action: dict, merged: dict) -> dict:
    """Replace every argument whose value is ``${account.email}`` with the
    connected account's email for the identity the call runs with (a
    Google tool wants ``user_google_email`` on every call; the page must
    never ask the viewer to type it). No account connected → the token
    stays, and the tool's own refusal names the problem."""
    if not any(v == ACCOUNT_EMAIL_TOKEN for v in merged.values()):
        return merged
    sub, _word = runs_as(row)
    email = connected_account(action.get("mcp") or "", row.get("agent") or "", sub)
    if not email:
        return merged
    return {k: (email if v == ACCOUNT_EMAIL_TOKEN else v) for k, v in merged.items()}
