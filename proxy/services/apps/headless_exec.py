"""Headless MCP tool execution for pinned app ``mcp_tool`` actions.

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
import hashlib
import json
import logging
import time
import uuid
from typing import TYPE_CHECKING

import config
from core import placement
from core.layers.direct.mcp import AgentMCPManager
from core.session import session_kind
from storage import db_apps

if TYPE_CHECKING:
    from core.config.task_config_builder import TaskIdentity

logger = logging.getLogger("claude-proxy.apps")

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


class _Entry:
    __slots__ = ("manager", "session_id", "scope_key", "mcps", "last_used",
                 "busy", "sem", "idle_s", "pinned_until")

    def __init__(self, manager: AgentMCPManager, session_id: str, scope_key: str,
                 mcps: frozenset[str]):
        self.manager = manager
        self.session_id = session_id
        self.scope_key = scope_key
        self.mcps = mcps
        self.last_used = time.monotonic()
        self.busy = 0
        self.sem = asyncio.Semaphore(CALL_CONCURRENCY)
        self.idle_s: float | None = None   # None → the platform idle timeout
        self.pinned_until = 0.0            # monotonic; LRU skips while pinned


PoolKey = tuple[str, str, str]

# (agent, scope_key, mcps_key) → entry. scope_key is "" for shared apps, the
# OWNER's sub for personal apps — the credential boundary lives in this key;
# mcps_key is the sorted MCP set the manager was built with.
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


def _pool_key(agent: str, row: dict, mcps: frozenset[str]) -> PoolKey:
    return (agent, _scope_key(row), _mcps_key(mcps))


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


def _bundles_with_token_files(agent: str, identity: "TaskIdentity", secret_bundles: dict | None,
                              mcps: frozenset[str]) -> dict:
    """The manifests' secret bundles for the MCPs this manager starts, with
    the token files of the app identity's OAuth stdio MCPs delivered the way
    the Claude and Codex layers deliver them (no token directory is mounted
    into a sandbox)."""
    from core.credentials import credential_files
    bundles = dict(secret_bundles or {})
    credential_files.deliver_token_files(
        bundles, agent, user_sub=identity.creds_user_sub or "", session_scope=identity.scope,
    )
    return {k: v for k, v in bundles.items() if k in mcps}


def _build_session_parts(agent: str, row: dict, mcps: frozenset[str], session_id: str = ""):
    """Blocking build (runs in a thread): MCP config for the app's identity,
    sandbox builder, and the SecurityContext to register. Mirrors
    ``task_config_builder`` (identity/visibility/path_env) and the Direct-LLM
    layer's ``start_session`` (sandbox mounts + stdio dir binds), restricted
    to the manifests whose mcpServers key is in ``mcps``: mounts, stdio dir
    binds and secret bundles cover only what this manager starts."""
    from auth.path_policy import SecurityContext
    from core.config.task_config_builder import resolve_task_identity
    from core.sandbox.sandbox import SandboxBuilder, SandboxMount, resolve_sandbox_config
    from core.layers.cli.config_dir import ensure_persistent_claude_dir
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

    # Task-context build (a button IS unattended machine-fired execution):
    # task_mode applies the manifests' task exclusions; identity.creds_user_sub
    # resolves per-user credentials for personal apps and the service accounts
    # for shared ones. is_remote=False fail-closed — device/satellite MCPs are
    # rejected at pin time and never reach this path.
    mcp_config, credential_env, _excl, secret_bundles, _bash = (
        mcp_registry.build_session_mcp_config(
            agent, identity.creds_user_sub,
            session_id=session_id,
            task_mode=True,
            task_scope=identity.scope,
            username=identity.username,
            user_role=identity.role,
            task_owner=identity.creds_user_sub or "",
            task_username=identity.username if identity.creds_user_sub else "",
            placement=placement.LOCAL_PLACEMENT,
        )
    )
    credential_env = dict(credential_env or {})
    secret_bundles = _bundles_with_token_files(agent, identity, secret_bundles, mcps)

    # Manifest-declared path_env values (same loop as task_config_builder).
    assigned = [
        m for m in (mcp_registry.get_agent_mcps(agent, placement=placement.LOCAL_PLACEMENT) or [])
        if (getattr(m, "server_name", "") or m.name) in mcps
    ]
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

    # An app button runs its MCPs in-process, the way the Direct LLM layer
    # does: it wants the ``.claude`` tree (the MCP config home the sandbox
    # mounts), not an engine — so it asks for that dir by name.
    host_claude_dir = ensure_persistent_claude_dir(
        agent,
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
        # PINNED False: app action clicks are not scheduled fires —
        # the knowledge_rw opt-in never applies here, whoever owns the app.
        knowledge_rw=False,
    )
    return mcp_config, secret_bundles, credential_env, sandbox_builder, ctx


async def _create_entry(agent: str, row: dict, scope_key: str,
                        mcps: frozenset[str]) -> _Entry:
    session_id = f"appx-{uuid.uuid4().hex[:12]}"
    mcp_config, bundles, credential_env, builder, ctx = await asyncio.to_thread(
        _build_session_parts, agent, row, mcps, session_id,
    )
    # Register the synthetic session BEFORE start: MCP subprocesses hold a
    # session JWT (minted in build_session_env) and their hook callbacks
    # (resolve-path, display, …) resolve through this context.
    from core.session.session_state import (
        set_session_security, _record_session_use, mark_headless_live,
    )
    set_session_security(session_id, ctx)
    mark_headless_live(session_id)
    _record_session_use(session_id, client_type=session_kind.APP.name, agent=agent)

    mgr = AgentMCPManager(
        agent,
        credential_env=credential_env,
        session_id=session_id,
        sandbox_builder=builder,
        prebuilt_config=(mcp_config, bundles),
        enable_http_transport=True,
        included_mcps=set(mcps),
    )
    try:
        async with _start_sem:
            await asyncio.wait_for(mgr.start(), timeout=START_TIMEOUT_S)
    except Exception:
        await _dispose(_Entry(mgr, session_id, scope_key, mcps))
        raise
    logger.info(
        f"Headless MCP manager started: agent={agent}, "
        f"scope={'personal' if scope_key else 'shared'}, "
        f"mcps={_mcps_key(mcps)}, session={session_id}"
    )
    return _Entry(mgr, session_id, scope_key, mcps)


async def _dispose(entry: _Entry) -> None:
    """Full synthetic-session teardown: close (terminates bwrap children)
    → permission-state cleanup (security ctx + brokered secrets). Each step
    best-effort so one failure never strands the rest."""
    from core.session.session_state import (
        cleanup_session_permission_state, clear_headless_live, mark_closing,
    )
    mark_closing(entry.session_id)
    clear_headless_live(entry.session_id)
    try:
        await entry.manager.close()
    except Exception:
        logger.exception("app exec: manager close failed")
    cleanup_session_permission_state(entry.session_id)


def _key_lock(key: PoolKey) -> asyncio.Lock:
    lock = _key_locks.get(key)
    if lock is None:
        lock = _key_locks[key] = asyncio.Lock()
    return lock


def _covering_entry(agent: str, scope_key: str, mcps: frozenset[str]) -> _Entry | None:
    """A warm manager of the same identity whose MCP set covers ``mcps`` —
    the exact set first, else the smallest superset."""
    exact = _pool.get((agent, scope_key, _mcps_key(mcps)))
    if exact is not None:
        return exact
    candidates = [
        e for k, e in _pool.items()
        if k[0] == agent and k[1] == scope_key and mcps <= e.mcps
    ]
    return min(candidates, key=lambda e: len(e.mcps)) if candidates else None


async def _get_entry(agent: str, row: dict, mcps: frozenset[str]) -> tuple[_Entry, bool]:
    """Warm entry covering ``mcps`` or a fresh build of exactly that set; the
    second element says WHICH (a fresh build that lacks the wanted tool must
    not be torn down and rebuilt — the MCP just failed to start)."""
    key = _pool_key(agent, row, mcps)
    async with _key_lock(key):
        entry = _covering_entry(agent, key[1], mcps)
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
            logger.info(f"Evicting headless MCP manager (pool cap): {oldest_key}")
            await _dispose(oldest)
        entry = await _create_entry(agent, row, key[1], mcps)
        _pool[key] = entry
        _ensure_sweeper()
        return entry, True


async def _drop_entry(key: PoolKey) -> None:
    async with _key_lock(key):
        entry = _pool.pop(key, None)
    if entry:
        if entry.busy:
            logger.warning("app exec: dropping a busy headless manager (self-heal)")
        await _dispose(entry)


async def _wait_idle(entry: _Entry) -> None:
    """Block until no call runs on the entry (bounded by the longest call a
    manager allows): a self-heal rebuild must never dispose a manager under
    a sibling's running tool."""
    deadline = time.monotonic() + entry.manager.tool_timeout + 30
    while entry.busy and time.monotonic() < deadline:
        await asyncio.sleep(0.1)


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


async def warm(row: dict) -> bool:
    """Build (or touch) the manager for the row's manifest set and keep it
    warm: idle grace and LRU pin for ``KEEP_WARM_S``. False when there is
    nothing to build (no ``mcp_tool`` action) or the identity fails closed.
    The route layer gates access and approval; the build is the same one a
    click would pay, so the click after this one is instant."""
    agent = row.get("agent") or ""
    mcps = manifest_mcps(row)
    if not mcps:
        return False
    if row.get("username"):
        ok = await asyncio.to_thread(db_apps.owner_holds_agent, agent, row.get("owner_sub") or "")
        if not ok:
            return False
    entry, _ = await _get_entry(agent, row, mcps)
    entry.idle_s = KEEP_WARM_S
    entry.pinned_until = time.monotonic() + KEEP_WARM_S
    return True


async def execute_app_tool(row: dict, action: dict, merged_args: dict) -> dict:
    """Execute one declared ``mcp_tool`` action with fully-validated, merged
    args. Returns ``{"status": "done", "result": <text>}`` when the tool ran
    (tool-level errors come back as the tool's own error text — the page
    renders it), or ``{"status": "error", "reason": …}`` for infrastructure
    failures. All AUTH gates ran in the route layer before this."""
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
            entry, created = await _get_entry(agent, row, mcps)
            if not entry.manager.has_tool(namespaced) and not created:
                # Self-heal: the MCP may have been (re-)enabled or crashed
                # since this WARM manager was built — rebuild with the union
                # of its set and this app's, and retry. Never on a fresh
                # build (the MCP just failed to start), at most once per
                # cooldown per key (a flaky MCP must not turn every click
                # into a full teardown+rebuild), and only once the entry is
                # idle: a batch's sibling calls may still be running on it.
                key = (agent, entry.scope_key, _mcps_key(entry.mcps))
                now = time.monotonic()
                if now - _selfheal_at.get(key, 0.0) >= _SELF_HEAL_COOLDOWN_S:
                    _selfheal_at[key] = now
                    logger.info(
                        f"app exec: self-heal rebuild for {key} "
                        f"(missing {namespaced})"
                    )
                    await _wait_idle(entry)
                    if _pool.get(key) is entry:
                        await _drop_entry(key)
                    entry, _ = await _get_entry(agent, row, entry.mcps | mcps)
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
                logger.error(f"app exec: dispatch timed out for {namespaced}")
                return {"status": "error", "reason": "The tool call timed out"}
            finally:
                entry.busy -= 1
            # A WARM manager whose server died under the call (the server
            # evicted the idle session while the manager sat in the pool):
            # one fresh manager and one more press, so the hourly wake
            # after a quiet stretch is not the one that fails.
            if attempt == 1 and not created and not entry.manager.has_tool(namespaced):
                key = (agent, entry.scope_key, _mcps_key(entry.mcps))
                logger.info(f"app exec: {namespaced} died under the call, rebuilding {key} once")
                try:
                    await _wait_idle(entry)
                    if _pool.get(key) is entry:
                        await _drop_entry(key)
                    entry, created = await _get_entry(agent, row, mcps)
                except Exception:
                    logger.exception(f"app exec: rebuild after a dead call failed for {agent}")
                    break
                if entry.manager.has_tool(namespaced):
                    continue
            break
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
    user, the agent's service binding for ``""`` — or ``""`` when none is
    connected. The same lookup the token map makes for a chat's
    ``${account.email}`` (services/mcp/dynamic_context.py)."""
    from services.oauth import credential_resolver
    from storage.identity import credential_store
    try:
        ref = credential_resolver.pick_account(mcp_name, agent, user_sub=user_sub)
    except Exception:
        logger.exception("app exec: account lookup failed for %s on %s", mcp_name, agent)
        return ""
    if ref is None:
        return ""
    try:
        accounts = credential_store.list_user_accounts(ref.owner_sub, mcp_name) or []
    except Exception:
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
