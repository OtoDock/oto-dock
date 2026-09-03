"""Headless MCP tool execution for pinned mini-app ``mcp_tool`` actions.

A declared, user-approved app button invokes ONE MCP tool directly — no agent
session, no LLM turn, no token spend. Execution reuses the Direct-LLM MCP
client (``core/layers/direct/mcp.py``) under a SYNTHETIC session identity:

* **Identity is the task posture, verbatim** (``resolve_task_identity``):
  a shared app runs agent-scoped (service credentials, role manager — admin
  only for admin-only agents, exactly what the task scheduler grants); a
  personal app runs as its OWNER (their per-agent role + per-user MCP
  credentials). Never wider than an unattended task of the same scope.
* **Sandbox parity is fail-closed**: stdio MCPs get the same per-MCP bwrap
  wrapping a Direct-LLM session builds (mounts + netns from
  ``resolve_sandbox_config``). If the sandbox build fails, the click is
  DENIED — never a fallback to an unwrapped subprocess.
* **One pooled ``AgentMCPManager`` per (agent, scope owner)** keeps the MCP
  subprocesses warm across clicks (first click pays the spawn latency). A
  personal app's manager carries the owner's credentials and its pool key
  carries the owner's sub, so it can never serve another user — the route
  layer already restricts personal apps to their owner (asserted here too).
* **Full-session teardown on reap**: OAuth writeback → close (terminates the
  bwrap children) → ``cleanup_session_permission_state`` (drops the security
  context + brokered secrets), so the synthetic session leaves nothing
  behind. Idle timeout is the platform ``session_idle_timeout`` knob.

The route layer (``api/apps/apps.py``) owns every AUTH gate — approval sig,
approver re-check, MCP-still-assigned, args-schema validation, rate limits.
This module owns identity synthesis and execution only.
"""

import asyncio
import hashlib
import json
import logging
import time
import uuid

import config
from core.layers.direct.mcp import AgentMCPManager

logger = logging.getLogger("claude-proxy.apps")

RESULT_MAX_CHARS = 32 * 1024
POOL_MAX = 8            # LRU-evicted; each entry is a set of MCP subprocesses
START_CONCURRENCY = 2   # simultaneous manager cold-starts (subprocess spawns)
START_TIMEOUT_S = 90    # backstop over mgr.start() — a wedged MCP handshake
                        # must surface as a click error, not a hung request

# A missing tool triggers ONE rebuild ("self-heal": the MCP may have been
# re-enabled or crashed since the manager warmed) — but a tool whose MCP
# flakes on EVERY start must not turn each click into a full manager
# teardown+rebuild. Found live 2026-07-10: uptime-kuma failing "Connection
# closed" on startup put the system-admin manager in a rebuild-per-refresh
# loop, ~15s each (unifi's connect retries dominate the start). Between
# rebuilds the missing tool fails fast with the same click error.
_SELF_HEAL_COOLDOWN_S = 120

_SWEEP_INTERVAL_S = 60


class _Entry:
    __slots__ = ("manager", "session_id", "scope_key", "last_used", "busy")

    def __init__(self, manager: AgentMCPManager, session_id: str, scope_key: str):
        self.manager = manager
        self.session_id = session_id
        self.scope_key = scope_key
        self.last_used = time.monotonic()
        self.busy = 0


# (agent, scope_key) → entry. scope_key is "" for shared apps, the OWNER's sub
# for personal apps — the credential boundary lives in this key.
_pool: dict[tuple[str, str], _Entry] = {}
_key_locks: dict[tuple[str, str], asyncio.Lock] = {}
_start_sem = asyncio.Semaphore(START_CONCURRENCY)
_inflight: set[tuple[str, str]] = set()  # (app_id, action_id|args-fp) — one at a time
_selfheal_at: dict[tuple[str, str], float] = {}  # last rebuild-for-missing-tool
_sweeper: asyncio.Task | None = None


def _scope_key(row: dict) -> str:
    return (row.get("owner_sub") or "") if row.get("username") else ""


def _personal_owner_ok(agent: str, owner_sub: str) -> bool:
    """Fail-closed guard for personal apps: the owner must STILL hold access
    to the agent. ``resolve_task_identity`` defaults an unassigned creator to
    "viewer" (tolerable for a task the creator authored end-to-end; too open
    for a standing no-LLM credential path)."""
    from storage import database as task_store
    if not owner_sub:
        return False
    u = task_store.get_user(owner_sub)
    if not u:
        return False
    if (u.get("role") or "") == "admin":
        return True
    return bool(task_store.get_user_agent_roles(owner_sub).get(agent, ""))


def _build_session_parts(agent: str, row: dict):
    """Blocking build (runs in a thread): MCP config for the app's identity,
    sandbox builder, and the SecurityContext to register. Mirrors
    ``task_config_builder`` (identity/visibility/path_env) and the Direct-LLM
    layer's ``start_session`` (sandbox mounts + stdio dir binds)."""
    from auth.path_policy import SecurityContext
    from core.config.task_config_builder import resolve_task_identity
    from core.sandbox.sandbox import SandboxBuilder, SandboxMount, resolve_sandbox_config
    from core.sandbox.session_config_dir import ensure_persistent_agent_dir
    from core.session.visibility import resolve_visibility
    from services import path_roles
    from services.mcp import mcp_registry
    from storage import agent_store

    scope = "user" if row.get("username") else "agent"
    identity = resolve_task_identity(agent, scope, row.get("owner_sub") or None)
    vis = resolve_visibility(
        agent,
        username=identity.username,
        user_role=identity.role,
        user_sub=identity.creds_user_sub or "",
        scope_override=identity.scope,
    )

    # Task-context build (a button IS unattended machine-fired execution):
    # task_mode applies the manifests' task exclusions; identity.creds_user_sub
    # resolves per-user credentials for personal apps and the service accounts
    # for shared ones. is_remote=False fail-closed — device/satellite MCPs are
    # rejected at pin time and never reach this path.
    mcp_config, credential_env, _excl, secret_bundles, _bash = (
        mcp_registry.build_session_mcp_config(
            agent, identity.creds_user_sub,
            task_mode=True,
            task_scope=identity.scope,
            username=identity.username,
            user_role=identity.role,
            task_owner=identity.creds_user_sub or "",
            task_username=identity.username if identity.creds_user_sub else "",
            is_remote=False,
        )
    )
    credential_env = dict(credential_env or {})

    # Manifest-declared path_env values (same loop as task_config_builder).
    assigned = mcp_registry.get_agent_mcps(agent, is_remote=False) or []
    for manifest in assigned:
        if not manifest.path_env:
            continue
        for env_var, decl in manifest.path_env.items():
            try:
                credential_env[env_var] = path_roles.resolve_path_env_entry(
                    decl, username=vis.mount_username, user_role=identity.role,
                )
            except ValueError as e:
                logger.warning(
                    "app exec: path_env injection failed for %s.%s: %s",
                    manifest.name, env_var, e,
                )

    host_claude_dir = ensure_persistent_agent_dir(
        agent, execution_path="direct-llm",
        username=vis.mount_username, scope=vis.mount_scope,
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
        _uv = config.MCPS_DIR.resolve() / ".uv-python"
        if _uv.is_dir():
            stdio_dirs.append(str(_uv))

    is_admin_agent = agent_store.is_admin_only(agent)
    sandbox_cfg = resolve_sandbox_config(
        role=identity.role,
        username=vis.mount_username,
        agent_name=agent,
        is_admin_agent=is_admin_agent,
        host_claude_dir=host_claude_dir,
        user_sub=identity.creds_user_sub or "",
        mcp_sandbox_mounts=mcp_mounts,
        config_visible=vis.config_visible,
        mount_shared=vis.mount_shared,
        mcp_dir_binds=stdio_dirs,
    )
    sandbox_builder = SandboxBuilder(sandbox_cfg)

    ctx = SecurityContext(
        role=identity.role,
        username=identity.username,   # REAL owner for attribution ("" shared)
        agent=agent,
        is_admin_agent=is_admin_agent,
        session_scope=vis.mount_scope,
        config_visible=vis.config_visible,
        available_scopes=vis.available_scopes,
        # PINNED False: mini-app action clicks are not scheduled fires —
        # the knowledge_rw opt-in never applies here, whoever owns the app.
        knowledge_rw=False,
    )
    return mcp_config, secret_bundles, credential_env, sandbox_builder, ctx


async def _create_entry(agent: str, row: dict, scope_key: str) -> _Entry:
    session_id = f"appx-{uuid.uuid4().hex[:12]}"
    mcp_config, bundles, credential_env, builder, ctx = await asyncio.to_thread(
        _build_session_parts, agent, row,
    )
    # Register the synthetic session BEFORE start: MCP subprocesses hold a
    # session JWT (minted in build_session_env) and their hook callbacks
    # (resolve-path, display, …) resolve through this context.
    from core.session.session_state import set_session_security, _record_session_use
    set_session_security(session_id, ctx)
    _record_session_use(session_id, client_type="app", agent=agent)

    mgr = AgentMCPManager(
        agent,
        credential_env=credential_env,
        session_id=session_id,
        sandbox_builder=builder,
        prebuilt_config=(mcp_config, bundles),
        enable_http_transport=True,
    )
    try:
        async with _start_sem:
            await asyncio.wait_for(mgr.start(), timeout=START_TIMEOUT_S)
    except Exception:
        await _dispose(_Entry(mgr, session_id, scope_key))
        raise
    logger.info(
        f"Headless MCP manager started: agent={agent}, "
        f"scope={'personal' if scope_key else 'shared'}, session={session_id}"
    )
    return _Entry(mgr, session_id, scope_key)


async def _dispose(entry: _Entry) -> None:
    """Full synthetic-session teardown: OAuth writeback → close (terminates
    bwrap children) → permission-state cleanup (security ctx + brokered
    secrets). Each step best-effort so one failure never strands the rest."""
    from core.credentials.credential_writeback import writeback_credential_dirs
    from core.session.session_state import cleanup_session_permission_state
    try:
        await writeback_credential_dirs(entry.session_id)
    except Exception:
        logger.exception("app exec: credential writeback failed")
    try:
        await entry.manager.close()
    except Exception:
        logger.exception("app exec: manager close failed")
    cleanup_session_permission_state(entry.session_id)


def _key_lock(key: tuple[str, str]) -> asyncio.Lock:
    lock = _key_locks.get(key)
    if lock is None:
        lock = _key_locks[key] = asyncio.Lock()
    return lock


async def _get_entry(agent: str, row: dict) -> tuple[_Entry, bool]:
    """Warm entry or a fresh build; the second element says WHICH (a fresh
    build that lacks the wanted tool must not be torn down and rebuilt —
    the MCP just failed to start)."""
    key = (agent, _scope_key(row))
    async with _key_lock(key):
        entry = _pool.get(key)
        if entry:
            entry.last_used = time.monotonic()
            return entry, False
        while len(_pool) >= POOL_MAX:
            # Prefer idle victims: disposing a manager mid-call kills its
            # bwrap children under the running tool. Only when EVERY entry
            # is busy does the cap win over the in-flight call.
            idle = [k for k in _pool if not _pool[k].busy]
            oldest_key = min(idle or _pool, key=lambda k: _pool[k].last_used)
            oldest = _pool.pop(oldest_key)
            logger.info(f"Evicting headless MCP manager (pool cap): {oldest_key}")
            await _dispose(oldest)
        entry = await _create_entry(agent, row, key[1])
        _pool[key] = entry
        _ensure_sweeper()
        return entry, True


async def _drop_entry(key: tuple[str, str]) -> None:
    async with _key_lock(key):
        entry = _pool.pop(key, None)
    if entry:
        if entry.busy:
            logger.warning("app exec: dropping a busy headless manager (self-heal)")
        await _dispose(entry)


def _ensure_sweeper() -> None:
    global _sweeper
    if _sweeper is None or _sweeper.done():
        _sweeper = asyncio.get_running_loop().create_task(_sweep_loop())


async def _sweep_once() -> None:
    """Reap managers idle past the platform idle timeout."""
    timeout = config.get_idle_timeout()
    now = time.monotonic()
    for key in list(_pool):
        entry = _pool.get(key)
        if not entry or entry.busy:
            continue
        if now - max(entry.last_used, entry.manager.last_activity) <= timeout:
            continue
        async with _key_lock(key):
            current = _pool.get(key)
            if current is not entry or entry.busy:
                continue
            _pool.pop(key, None)
        logger.info(f"Reaping idle headless MCP manager: {key}")
        await _dispose(entry)


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


async def execute_app_tool(row: dict, action: dict, merged_args: dict) -> dict:
    """Execute one declared ``mcp_tool`` action with fully-validated, merged
    args. Returns ``{"status": "done", "result": <text>}`` when the tool ran
    (tool-level errors come back as the tool's own error text — the page
    renders it), or ``{"status": "error", "reason": …}`` for infrastructure
    failures. All AUTH gates ran in the route layer before this."""
    agent = row.get("agent") or ""
    app_id = row.get("id") or ""
    action_id = action.get("id") or ""
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
                _personal_owner_ok, agent, row.get("owner_sub") or "",
            )
            if not ok:
                return {"status": "error",
                        "reason": "Approval stale — the app owner no longer has access to this agent"}

        namespaced = f"mcp__{action.get('mcp')}__{action.get('tool')}"
        try:
            entry, created = await _get_entry(agent, row)
            if not entry.manager.has_tool(namespaced) and not created:
                # Self-heal: the MCP may have been (re-)enabled or crashed
                # since this WARM manager was built — rebuild and retry.
                # Never on a fresh build (the MCP just failed to start), and
                # at most once per cooldown per key (a flaky MCP must not
                # turn every click into a full teardown+rebuild).
                key = (agent, entry.scope_key)
                now = time.monotonic()
                if now - _selfheal_at.get(key, 0.0) >= _SELF_HEAL_COOLDOWN_S:
                    _selfheal_at[key] = now
                    logger.info(
                        f"app exec: self-heal rebuild for {key} "
                        f"(missing {namespaced})"
                    )
                    await _drop_entry(key)
                    entry, _ = await _get_entry(agent, row)
        except Exception as e:
            logger.exception(f"app exec: manager start failed for {agent}")
            return {"status": "error", "reason": f"Could not start the tool's MCP: {e}"}
        if not entry.manager.has_tool(namespaced):
            logger.warning(
                f"app exec: tool unavailable: app={row.get('slug')}, "
                f"tool={namespaced} (its MCP likely failed to start — "
                f"see mcp-manager errors above)"
            )
            return {"status": "error",
                    "reason": f"Tool '{action.get('tool')}' is not available on MCP '{action.get('mcp')}'"}
        # scope_key is derived from the row on BOTH sides; this assert is the
        # credential-boundary belt for future refactors.
        assert entry.scope_key == _scope_key(row), "headless pool scope mismatch"

        entry.busy += 1
        entry.last_used = time.monotonic()
        try:
            # execute_tools bounds the CALL (TOOL_CALL_TIMEOUT, surfaced as
            # error text); this outer margin only covers a wedged dispatch
            # layer — the click must ALWAYS get a terminal result.
            from core.layers.direct.mcp import TOOL_CALL_TIMEOUT
            results = await asyncio.wait_for(
                entry.manager.execute_tools(
                    [{"id": "app-action", "name": namespaced, "input": merged_args}],
                ),
                timeout=TOOL_CALL_TIMEOUT + 30,
            )
        except asyncio.TimeoutError:
            logger.error(f"app exec: dispatch timed out for {namespaced}")
            return {"status": "error", "reason": "The tool call timed out"}
        finally:
            entry.busy -= 1
        text = (results[0].get("content") if results else "") or "(empty result)"
        if len(text) > RESULT_MAX_CHARS:
            text = text[:RESULT_MAX_CHARS] + "\n… (result truncated)"
        logger.info(
            f"App tool fired: app={row.get('slug')}, action={action_id}, "
            f"tool={namespaced}, scope={'personal' if row.get('username') else 'shared'}"
        )
        return {"status": "done", "result": text}
    finally:
        _inflight.discard(flight)
