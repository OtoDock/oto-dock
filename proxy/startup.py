"""Application lifespan: startup orchestration + graceful shutdown.

Extracted from ``app.py`` — ``app = FastAPI(lifespan=lifespan)`` wires this in.
Boots the sandbox/quotas preflights, DB schema, background workers, reapers and
sweepers on startup, and drains sessions + workers on shutdown.
"""

import asyncio
import contextlib
import logging
import os
import threading
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager

from fastapi import FastAPI

import config
from services.scheduler import scheduler
from services.notifications import notification_manager
from storage import database as task_store
from storage import pg as pg_pool
from storage import schema as pg_schema
from core.session.session_state import set_pump_callbacks
from core.events.stream_pump import _active_pumps
from core.layers.cli import reap_idle_sessions
from core.layers.direct import reap_idle_direct_sessions
from core.layers.codex import reap_idle_codex_sessions

logger = logging.getLogger("claude-proxy")

# Background tasks the lifespan owns — cancelled in ``_shutdown_cleanup`` so
# their teardown never races the closed DB pool.
_bg_tasks: list[asyncio.Task] = []

# Hard-exit failsafe (armed as the LAST shutdown step): non-daemon threads —
# asyncio's default-executor workers above all — are joined by the interpreter
# AFTER uvicorn logs "Finished server process"; one blocked worker used to
# hang every stop until docker's 10s SIGKILL (exit 137). Tests flip the flag.
_ARM_EXIT_FAILSAFE = True
_EXIT_FAILSAFE_GRACE_S = 3.0

# Open descriptors the proxy asks for. The systemd and Docker default soft
# limit is 1024 (hard 524288): about a thousand idle sockets, or a few dozen
# Direct-LLM sessions (~2 fds per stdio MCP), and uvloop silently drops every
# new connection. Capped so children that close-loop up to their soft limit
# stay cheap.
_NOFILE_TARGET = 65536
_NOFILE_WARN_BELOW = 8192


def raise_nofile_limit(target: int = _NOFILE_TARGET) -> int | None:
    """Raise the soft RLIMIT_NOFILE to ``min(hard, target)``, never lowering
    it (a unit or compose file that already grants more keeps it). Returns
    the effective soft limit (None where the platform has no rlimits)."""
    try:
        import resource
    except ImportError:
        return None
    soft, hard = resource.getrlimit(resource.RLIMIT_NOFILE)
    want = target if hard == resource.RLIM_INFINITY else min(hard, target)
    if soft != resource.RLIM_INFINITY and want > soft:
        try:
            resource.setrlimit(resource.RLIMIT_NOFILE, (want, hard))
            soft = want
        except (ValueError, OSError) as e:
            logger.warning("Could not raise the open-file limit from %s: %s", soft, e)
    if soft != resource.RLIM_INFINITY and soft < _NOFILE_WARN_BELOW:
        logger.warning(
            "Open-file limit is %s (hard %s): below %d, the proxy runs out of "
            "descriptors well before 100 sessions. Raise LimitNOFILE (systemd) "
            "or ulimits.nofile (compose).", soft, hard, _NOFILE_WARN_BELOW)
    else:
        logger.info("Open-file limit: %s (hard %s)", soft, hard)
    return soft


def set_default_executor(loop: asyncio.AbstractEventLoop) -> ThreadPoolExecutor:
    """Size the loop's default executor (``asyncio.to_thread``) explicitly:
    left alone it is ``min(32, cpus + 4)``, and slow jobs (image pulls, a PTY
    reap, an HTTP fetch) queue ahead of file reads and bcrypt. More threads
    also mean more GIL contention for the loop, so it is not open-ended."""
    import config
    ex = ThreadPoolExecutor(max_workers=max(1, config.DEFAULT_EXECUTOR_WORKERS),
                            thread_name_prefix="asyncio")
    loop.set_default_executor(ex)
    return ex


# A path no route serves: the warm-up matches every route against it.
_WARMUP_PATH = "/__otodock_route_warmup__/x"


def warm_routes(app: FastAPI) -> None:
    """Build FastAPI's per-route state before the server listens. It builds
    each included router's effective routes lazily, on the first request that
    falls through them (0.38 s of loop on this proxy's routes). Every
    top-level route is matched on its own (the router's dispatch would stop
    at the SPA catch-all's full match), for an http and a websocket scope;
    no handler runs, and a route that fails to match is skipped."""
    common = {
        "path": _WARMUP_PATH, "raw_path": _WARMUP_PATH.encode(), "root_path": "",
        "query_string": b"", "headers": [], "server": ("127.0.0.1", 80),
        "client": ("127.0.0.1", 0), "app": app,
    }
    scopes = (
        {"type": "http", "method": "GET", "http_version": "1.1", "scheme": "http", **common},
        {"type": "websocket", "subprotocols": [], "scheme": "ws", **common},
    )
    for route in list(app.router.routes):
        for scope in scopes:
            try:
                route.matches(dict(scope))
            except Exception:
                logger.debug("route warm-up skipped %r", route, exc_info=True)

_SWEEP_CONCURRENCY = 4
_sweeps_running: dict[str, asyncio.Task] = {}


async def run_sweep_items(items) -> list[str]:
    """One tick of the 60 s sweep: every item not still running from the
    last tick, ``_SWEEP_CONCURRENCY`` at a time, each failure logged on its
    own. Returns the names that ran once they all ended (the loop does not
    wait for that: a tick is its own task)."""
    slot = asyncio.Semaphore(_SWEEP_CONCURRENCY)
    ran: list[str] = []

    async def _one(name: str, fn) -> None:
        async with slot:
            try:
                await fn()
            except Exception:
                logger.exception("%s failed", name)

    tasks = []
    for name, fn in items:
        previous = _sweeps_running.get(name)
        if previous is not None and not previous.done():
            logger.warning("%s still running from the last tick; skipped", name)
            continue
        ran.append(name)
        task = asyncio.create_task(_one(name, fn))
        _sweeps_running[name] = task
        tasks.append(task)
    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)
    return ran


def _purge_mcp_build_outputs() -> None:
    """The session MCP config build outputs are rebuilt per session; what an
    earlier release left there carried vendor bearers inline, so the two
    directories are emptied at every boot (the gateway descriptors beside
    them are kept: a re-adoption reads them)."""
    import shutil
    for name in ("user-mcp-configs", "sse-mcp-configs"):
        shutil.rmtree(config.SESSIONS_DIR / name, ignore_errors=True)


@asynccontextmanager
async def lifespan(app: FastAPI):
    # SIGUSR1 → all-thread stack dump on stderr; the field tool for shutdown
    # hangs ("Finished server process" logged but the process never exits).
    try:
        import faulthandler
        import signal
        faulthandler.register(signal.SIGUSR1, all_threads=True)
    except (ValueError, AttributeError, OSError):
        pass  # non-main thread or no SIGUSR1 on this platform

    raise_nofile_limit()
    default_executor = set_default_executor(asyncio.get_running_loop())
    _purge_mcp_build_outputs()

    # Startup — register client adapters
    from adapters import register_adapter
    from adapters.phone import PhoneAdapter
    from adapters.dashboard import DashboardAdapter
    register_adapter(PhoneAdapter())
    register_adapter(DashboardAdapter())

    # Register pump callbacks so scheduler / session_state can push events
    # and queue prompts on active pumps without circular imports.
    def _push_to_pump(chat_id: str, event: dict) -> bool:
        pump = _active_pumps.get(chat_id)
        if pump:
            try:
                return pump.push_ws_event(event)
            except Exception:
                pass
        return False

    def _queue_on_pump(chat_id: str, text: str, system: bool = False) -> bool:
        pump = _active_pumps.get(chat_id)
        if pump and not pump.is_done:
            # Only pumps whose producer DRAINS system_queue may accept —
            # a task/scheduler pump would swallow the prompt forever while
            # the delivery ladder thinks it succeeded (silent-loss hole
            # from the 2026-09-02 delegate trace). Refusal makes the
            # caller fall through to persistent/one-shot/direct delivery.
            # A person's message never comes this way: the chat owns its
            # queue (core/events/input_queue.py).
            if not system or not getattr(pump, "system_queue_consumer", False):
                return False
            pump.system_queue.append(text)
            return True
        return False

    def _inject_to_pump(chat_id: str, event) -> bool:
        # Inject a CommonEvent into the pump's event stream (processed in-order
        # → persisted in the turn + live-state). Used for proxy-emitted
        # delegate_spawn at task-create time.
        pump = _active_pumps.get(chat_id)
        if pump and not pump.is_done:
            try:
                pump.event_queue.put_nowait(event)
                return True
            except Exception:
                pass
        return False

    set_pump_callbacks(_push_to_pump, _queue_on_pump, _inject_to_pump)

    # Sandbox network-isolation preflight (isolation is mandatory): hard-fail
    # boot if pasta/ip/bwrap/launcher are missing OR the host can't create an
    # unprivileged user+net namespace (never silently run agents un-isolated),
    # and materialize the stub-resolver resolv.conf swap so every later sandbox
    # build is a pure argv transform.
    from core.sandbox.sandbox import (
        netns_preflight, cli_version_preflight, tmpfs_cap_preflight,
    )
    netns_preflight()

    # CLI pin drift check (warn-only): if the proxy host's claude/codex differ
    # from VERSIONS.md, log it so an operator can reconcile. Satellites self-heal
    # on auth; the proxy host is fixed by re-running the installer.
    cli_version_preflight()

    # Per-sandbox /tmp cap: probe bwrap --size once and log the effective cap
    # (or that this bubblewrap cannot cap), so it is visible in the boot log.
    tmpfs_cap_preflight()

    # Fail fast with an actionable message if the agents dir isn't writable by
    # the runtime user. In the containerised (uid 1000) deployment the named
    # volume is created root-owned; the compose init sidecar chowns it to the
    # proxy's uid before boot. Without this, the first workspace write would
    # EACCES deep in the call stack — surface the real cause here instead.
    if config.AGENTS_DIR.exists() and not os.access(config.AGENTS_DIR, os.W_OK):
        _uid = os.getuid() if hasattr(os, "getuid") else "?"
        raise RuntimeError(
            f"Agents dir {config.AGENTS_DIR} is not writable by uid {_uid}. In "
            f"Docker, ensure the init sidecar chowns the agents volume to the "
            f"proxy's uid (see docker-compose.yml)."
        )

    # Storage-quota preflight: resolve hard-vs-soft enforcement once. Hard XFS
    # enforcement auto-activates when the agents dir is on an XFS mount with
    # project quota active + the oto-quota helper is reachable; anything else
    # degrades to the soft (measure + warn) tier with a log line. Never bricks
    # boot — the soft tier is always a valid mode.
    from services.infra.storage_quota import quotas_preflight
    quotas_preflight()

    # Initialize PostgreSQL schema (all tables + migrations). One
    # transaction; its DDL may wait on locks a running pg_dump holds, so the
    # pool's statement timeout does not apply to it, and it waits longer for
    # a connection than a request would (Postgres may still be starting).
    with pg_pool.get_conn(timeout=60) as _pg_conn:
        _pg_conn.execute("SET LOCAL statement_timeout = 0")
        pg_schema.init_schema(_pg_conn)
        pg_schema.run_migrations(_pg_conn)
        _pg_conn.commit()
        # Drift guard: one loud ERROR when the live DB is missing columns the
        # code expects (a CREATE TABLE-only addition never reaches existing
        # installs) instead of scattered UndefinedColumn 500s at query time.
        pg_schema.check_schema_drift(_pg_conn)
    # Network self-check (T2 tripwire): warn if a control plane was re-added to
    # the shared network (undoing the compose split). No-op on bare metal.
    from services.infra.network_selfcheck import network_selfcheck
    await asyncio.to_thread(network_selfcheck)
    # Token files live only in the central store; a copy found in an agent
    # tree goes once, before any session could mount the tree.
    from services.oauth import credential_resolver as _cred_resolver
    try:
        await asyncio.to_thread(_cred_resolver.purge_agent_tree_credentials)
    except Exception:
        logger.exception("agent-tree credentials sweep failed (continuing)")
    # Offboarding: a person's live sessions close with their access, their
    # automations follow the admin who took it away, and the service
    # bindings they lent follow their standing.
    from services.agents import offboarding_bindings, offboarding_sessions, offboarding_transfer
    offboarding_sessions.register()
    offboarding_transfer.register()
    offboarding_bindings.register()
    from ws import dashboard as _dashboard_ws
    _dashboard_ws.register_offboarding()
    from services.apps import app_lifecycle as _app_lifecycle
    _app_lifecycle.register()
    # Credential-key canary: one loud ERROR when stored secrets can't be
    # decrypted (JWT_SECRET changed / config.env recreated) instead of
    # scattered use-time 500s and silently-empty credential reads.
    from storage.identity.credential_store import startup_key_canary
    startup_key_canary()
    # First-install only: default the platform timezone to the server's local
    # wall clock (UTC otherwise). Guarded on the setting being absent → set once,
    # never overwritten by later updates or an admin's change.
    from storage import database as _db_seed
    _db_seed.seed_platform_timezone_if_unset()
    # Department edge recompile at boot: compiled edges otherwise refresh only
    # on a department/assignment write, so a semantics change in the compiler
    # (e.g. the 2026-09-02 symmetric-adjacent upgrade) would never materialize
    # on an existing install. Idempotent, DB-only, O(agents+edges); also
    # self-heals drift from restored backups.
    try:
        from services.departments import edge_compiler as _edge_compiler
        _edge_compiler.recompile()
    except Exception:
        logger.exception("startup department edge recompile failed (non-fatal)")
    # Startup recovery: PARK recovery-eligible in-flight runs (remote CLI,
    # pinned target) for satellite re-adopt (Mode C) instead of blind-failing
    # them; every other orphaned run + all orphaned meetings are failed so
    # they don't appear stuck. Must run BEFORE the satellite heartbeat monitor
    # (below) so a run is parked before its satellite can report it.
    from services.scheduler import run_recovery
    parked_runs, orphaned_runs = run_recovery.defer_orphaned_runs()
    orphaned_meetings = task_store.mark_orphaned_meetings_failed()
    if parked_runs or orphaned_runs or orphaned_meetings:
        logger.info(
            f"Startup recovery: parked {parked_runs} run(s) for re-adopt, "
            f"marked {orphaned_runs} orphaned run(s) and "
            f"{orphaned_meetings} orphaned meeting(s) as failed"
        )
    # Startup recovery: reload the persisted per-session SecurityContext
    # index so a session that survived a proxy crash on a satellite (and its
    # background sub-agents) keeps full path-policy enforcement at the permission
    # gate instead of being denied as context-less. Contexts carry no secrets;
    # closed sessions were cleared, so a replayed dead-session JWT stays denied.
    from core.session.session_state import load_session_security
    load_session_security()
    # The sign-ins a logout revoked survive a restart (auth/session_revocation.py).
    from auth import session_revocation
    await asyncio.to_thread(session_revocation.load)

    from storage.agents import agent_store
    # Converge pre-1.4 agents on the agent.md persona filename before any
    # session builds a prompt (readers accept both names; this makes disk,
    # git and satellite sync agree on the new one).
    migrated = await asyncio.to_thread(agent_store.migrate_persona_filenames)
    if migrated:
        logger.info("Persona filename migration: %d agent(s) converged", migrated)
    # App releases: a row whose release copy is gone falls back to the
    # working file; a release directory with no row is removed.
    try:
        from services.apps import releases as _releases
        await asyncio.to_thread(_releases.reconcile)
    except Exception:
        logger.exception("App release reconcile failed (continuing)")
    # The platform catalog: remembers the loop for deltas raised off it and
    # listens for finished task runs.
    try:
        from api.apps import catalog as _catalog
        _catalog.install()
    except Exception:
        logger.exception("Platform catalog install failed (continuing)")
    # Checks (CHECKS.md): the one turn_end handler that judges a session's
    # work; nothing runs until a check is attached to an agent or a chat.
    try:
        from services.checks import evaluator as _checks
        _checks.install()
    except Exception:
        logger.exception("Checks install failed (continuing)")
    # Template apps seeded per member (COMMUNITY-AGENTS-REGISTRY.md): the
    # attach hook queues, this worker deploys.
    try:
        from services.community import template_app_seeder as _seeder
        _seeder.install()
    except Exception:
        logger.exception("Template app seeder install failed (continuing)")
    # App handlers (APPS.md "Handlers"): deliveries left in flight at the
    # crash go back to the queue, and due rows wake their apps.
    try:
        from services.apps import app_handlers as _handlers
        _bg_tasks.append(asyncio.create_task(_handlers.boot()))
    except Exception:
        logger.exception("App handlers boot failed (continuing)")
    # Managed/cloud installs: seed the bootstrap license key ONCE (when the DB
    # has none). The license_check_worker then owns updates via the relay
    # re-issue (adopt-on-check) — a worker-adopted key is never overwritten.
    from auth import license as L
    if config.OTODOCK_LICENSE_KEY and not L.get_license_key():
        L.set_license_key(config.OTODOCK_LICENSE_KEY)
        logger.info("Seeded license_key from OTODOCK_LICENSE_KEY (bootstrap)")
    # Phone server: seed default settings and routes if tables are empty
    from services.phone.phone_config_seed import seed_phone_config
    seed_phone_config()
    # MCP Framework: discover manifests, seed DB, start Docker MCPs
    from services.mcp import mcp_registry
    from services.mcp import docker_manager
    from services.mcp import mcp_venv_bootstrap
    mcp_registry.scan_manifests()
    # Ensure every bundled Python MCP has a current venv before any session
    # tries to launch it. Idempotent — fast no-op when every venv is fresh.
    # Closes the gap discovered with image-search-mcp where a freshly-pulled
    # MCP had no venv on disk and silently failed to start.
    try:
        venv_results = await mcp_venv_bootstrap.ensure_bundled_venvs_at_startup()
        built = [n for n, r in venv_results.items() if r == "ok"]
        failed = [n for n, r in venv_results.items() if r in ("failed", "exception")]
        if built:
            logger.info("Bundled MCP venvs built: %s", built)
        if failed:
            logger.warning("Bundled MCP venv build failures: %s", failed)
    except Exception:
        logger.exception("Bundled MCP venv bootstrap failed (non-fatal)")

    # Docker MCP bring-up runs in the BACKGROUND: on a fresh box it pulls
    # multi-GB images (file-tools → Collabora), and doing that synchronously
    # here blocked the HTTP bind for minutes with the service reading as
    # `active` but unbound — indistinguishable from a hang. Sessions that
    # start before a container is up get connect-refused tool errors until it
    # is (same behavior as a boot where docker was unavailable); the pull
    # progress itself is logged by docker_manager.
    async def _docker_mcp_bringup() -> None:
        try:
            await asyncio.to_thread(docker_manager.startup_docker_mcps)
        except Exception:
            logger.exception("Docker MCP bring-up failed (non-fatal)")

    _bg_tasks.append(asyncio.create_task(_docker_mcp_bringup()))
    # NOTE: there is deliberately NO boot backfill of core MCPs onto existing
    # agents (removed 2026-07-26; it ran every boot until then). Core MCPs are
    # assigned at exactly two moments — agent creation
    # (``api/agents/agents.py:create_agent``) and template install
    # (``community_agent_installer`` step 4c) — so a per-agent removal or a
    # template's ``core_mcps: "none"`` opt-out sticks across restarts. The
    # trade-off, accepted with the core set considered complete: a core MCP
    # introduced by a FUTURE version reaches only agents created after the
    # upgrade; pre-existing agents pick it up via re-install or a release-note
    # instruction, never silently.
    # Create/remove the platform-managed ("Hosted by OtoDock") system
    # instances to match the hosted-relay state — relay usable AND the master
    # toggle on AND connected (relay_client.system_relay_active()). Extracted so
    # the connect/disconnect handlers can re-run it live (the toggle is a runtime
    # change; startup-only gating would strand stale instances). Idempotent +
    # wrapped so a throw can't abort boot.
    try:
        from services.billing import hosted_instances
        hosted_instances.reconcile_otodock_system_instances()
    except Exception:
        logger.exception("hosted instance reconcile failed (non-fatal)")
    # PA-lite is auto-installed by api/auth/setup.py::setup_first_user the moment
    # the owner admin completes the setup wizard — runs with admin identity
    # so all required MCPs auto-install inline (no requests). The proxy
    # startup deliberately does NOT trigger the install here; without a
    # user identity the install couldn't auto-approve MCPs and would leave
    # the agent half-configured.
    # Reset leaked subscription session counters from previous run
    from storage.billing import subscription_store
    subscription_store.reset_active_sessions()
    # Prune crash-orphaned session→subscription bindings (release never ran).
    # Bounded staleness: rows younger than the TTL deliberately SURVIVE the
    # restart — they are what keeps usage attribution + scope-sticky selection
    # working for sessions that outlive the proxy process (remote satellites,
    # re-adopted chats). See subscription_pool.get_session_subscription.
    try:
        pruned = subscription_store.prune_stale_session_bindings()
        if pruned:
            logger.info(f"Pruned {pruned} stale session-subscription binding(s)")
        # Rows whose subscription is gone (deleted or replaced while a
        # session was bound) — no TTL saves them: they would bind a
        # re-adopted session to nothing and attribute usage to a ghost.
        orphans = subscription_store.prune_orphan_session_bindings()
        if orphans:
            logger.info(f"Pruned {orphans} orphan session-subscription binding(s)")
    except Exception:
        logger.exception("session-binding prune failed (non-fatal)")
    # Sync built-in models into execution_layer_models so the DB fallback in
    # config.resolve_agent_model() can find them without requiring admin to
    # visit Admin > Execution Layers first. Idempotent — builtin rows follow the
    # registry (release price/model updates propagate); admin enable/disable is
    # preserved (custom pricing lives on custom models).
    from core.session.session_manager import get_all_capabilities
    _caps = get_all_capabilities()
    for _path, _layer_caps in _caps.items():
        subscription_store.sync_builtin_models(_path, _layer_caps.get("models", []))
    # Retired-builtin remaps (config.MODEL_SUCCESSORS): one-time lossless
    # moves of every persisted pin of a retired id onto its successor —
    # without them a pinned agent's NEW chats would silently fall back to the
    # layer's first enabled model because the retired builtin row no longer
    # passes the model-allowed check. Registry-driven: a new retirement is one
    # MODEL_SUCCESSORS entry, no code here. The walk (remap_retired_models)
    # lands each retired id on the END of its chain, so the dict's order
    # cannot strand a pin on an intermediate retired id, and leaves alone an
    # id an admin has re-added as a custom row (the admin chose to keep it —
    # the walk runs on every boot and would otherwise rewrite those pins at
    # each restart). Idempotent; must never kill boot.
    try:
        subscription_store.remap_retired_models(config.MODEL_SUCCESSORS)
    except Exception:
        logger.exception("retired-model remaps failed (non-fatal)")
    logger.info(f"Synced builtin models for {len(_caps)} execution layer(s)")
    # Initialize concurrency control (reads limits from DB)
    from core import concurrency
    concurrency.init()
    scheduler.start()
    # Initialize notification system (after scheduler so it can register jobs)
    notification_manager.start()
    _bg_tasks.append(asyncio.create_task(reap_idle_sessions()))
    logger.info(
        f"Persistent session reaper started "
        f"(timeout={config.PERSISTENT_SESSION_TIMEOUT}s)"
    )
    _bg_tasks.append(asyncio.create_task(reap_idle_direct_sessions()))
    logger.info(
        f"Direct session reaper started "
        f"(timeout={config.DIRECT_SESSION_TIMEOUT}s)"
    )
    _bg_tasks.append(asyncio.create_task(reap_idle_codex_sessions()))
    logger.info("Codex session reaper started")
    from core.remote.remote_execution import reap_idle_remote_sessions
    _bg_tasks.append(asyncio.create_task(reap_idle_remote_sessions()))
    logger.info("Remote session reaper started")
    # The credential gateway's push loop: a machine session's vendor tokens
    # are renewed under their lease and wiped at the session's purge.
    from core.credentials import mcp_gateway as _mcp_gateway
    from core.remote import mcp_gateway_push as _mcp_gateway_push
    _mcp_gateway.add_purge_hook(_mcp_gateway_push.on_purge)
    _bg_tasks.append(asyncio.create_task(_mcp_gateway_push.run_push_loop()))
    logger.info("MCP gateway push loop started")
    # Task ("is_task") session-index entries are popped on run completion in
    # scheduler._run_task; this is the backstop for any that leak (pre-launch
    # failure / restart mid-run) so the index can't grow unbounded.
    from core.session.session_state import reap_idle_task_sessions
    _bg_tasks.append(asyncio.create_task(reap_idle_task_sessions()))
    logger.info("Task session reaper started")
    # Interactive CLI (PTY-backed) sessions: idle reaper spares viewed/active
    # sessions; the 120s slot reconciler already counts them live.
    from core.session import interactive_session
    interactive_session.start_idle_reaper()
    logger.info("Interactive CLI idle reaper started")
    from core.concurrency import _reconciliation_loop, maintenance_loop
    _bg_tasks.append(asyncio.create_task(_reconciliation_loop()))
    logger.info("Chat slot reconciliation started (120s interval)")
    _bg_tasks.append(asyncio.create_task(maintenance_loop()))
    logger.info("Concurrency maintenance loop started (parked-task wakeups + eviction)")
    from core.session import prewarm_session_registry
    _bg_tasks.append(asyncio.create_task(prewarm_session_registry.reap_loop()))
    logger.info("Pre-warm TTL reaper started")
    from services.knowledge import library_projector
    _bg_tasks.append(asyncio.create_task(library_projector.reconcile_loop()))
    logger.info("Knowledge-library reconcile sweep started (300s interval)")
    # App servers (APPS.md): idle stop, backoff bookkeeping, rows gone.
    from services.apps import app_supervisor
    _bg_tasks.append(asyncio.create_task(app_supervisor.sweep_loop()))
    logger.info("App supervisor sweep started (30s interval)")
    # Start satellite heartbeat monitor
    from core.remote.satellite_connection import get_connection_manager
    _sat_cm = get_connection_manager()
    # Wire Mode C run-recovery: the satellite's post-auth sessions_alive report
    # re-adopts parked in-flight runs. Registered before the heartbeat monitor
    # starts so the first reconnect's report is handled.
    run_recovery.register(_sat_cm)
    _bg_tasks.append(asyncio.create_task(_sat_cm.heartbeat_monitor()))
    logger.info("Satellite heartbeat monitor started")

    # Delegate wakes parked across the restart: redeliver once boot settles.
    # Delayed so satellites get their reconnect window first — a wake whose
    # machine is still offline just re-persists and the machine's own
    # reconnect pass (run_recovery.on_sessions_alive) retries it.
    async def _boot_wake_redelivery() -> None:
        await asyncio.sleep(15)
        try:
            woken = await scheduler.redeliver_pending_wakes()
            if woken:
                logger.info("Startup wake redelivery: woke %d chat(s)", woken)
        except Exception:
            logger.exception("Startup wake redelivery sweep failed")
    _bg_tasks.append(asyncio.create_task(_boot_wake_redelivery()))

    # Start the HTTP-over-WS tunnel dispatcher (sweeps leaked streams,
    # owns the singleton httpx.AsyncClient).
    from core.remote.satellite_http_tunnel import get_dispatcher
    await get_dispatcher().start()
    logger.info("Satellite HTTP tunnel dispatcher started")

    # Periodic sweeper for in-flight warmup + install registry entries.
    # Belt-and-suspenders behind the try/finally blocks in _handle_warmup
    # (warmup) and RemoteExecutionLayer.start_session (install); in
    # practice those handle all real paths and the sweep only kicks in
    # if a code path is added that forgets to unregister.
    from core.session import warmup_registry as _warmup_registry
    from core.remote import install_registry as _install_registry

    # Wire install-progress delivery to the per-user dashboard notify channel
    # (the same path satellite-update events use). install_registry stays in
    # core and never imports the ws layer; the broadcaster is injected here so
    # every install event reaches all the owning user's dashboard tabs, immune
    # to the per-connection attach races that previously hid the install bar.
    from ws.satellite import push_install_event as _push_install_event
    _install_registry.set_broadcaster(_push_install_event)

    # Workspace transfer progress (Feature E) rides the same delivery model.
    from core.remote import transfer_registry as _transfer_registry
    from ws.satellite import push_transfer_event as _push_transfer_event
    _transfer_registry.set_broadcaster(_push_transfer_event)

    # The 60 s sweep's items, each isolated (one that raises is logged and
    # the next runs) and run concurrently, _SWEEP_CONCURRENCY at a time: a
    # slow one no longer pushes the rest back, and an item still running
    # from the last tick is skipped (a hung sweep holds one slot, never
    # four, and the fingerprint merge never runs twice). Every store touch
    # goes through run_db (storage/pg.py's event-loop rule): a periodic
    # DELETE + COMMIT on the loop is exactly what froze the proxy on
    # 2026-09-03 when the disk went slow; the two file sweeps run in a
    # worker thread for the same reason.
    from storage.pg import run_db as _run_db

    async def _sweep_media():
        from services.media import media_pipeline as _mp
        from storage import database as _db
        await asyncio.to_thread(_mp.sweep_host_media_cache)      # TTL satellite-host media
        await _run_db(_db.sweep_expired_media_tokens)  # reap expired workspace tokens

    async def _sweep_preview_snapshots():
        # Version-pinned preview snapshots: reap dirs of deleted chats
        # (internally throttled to hourly).
        from services.media import preview_snapshots as _psnap
        await asyncio.to_thread(_psnap.sweep_orphans)

    async def _sweep_recover_bin():
        # Workspace Recover Bin: reap entries past their 7-day TTL
        # (DB rows + on-disk bytes). Quick indexed delete, usually 0.
        from storage.files import recover_bin_store as _rbstore
        await _run_db(_rbstore.delete_expired)

    async def _sweep_tombstones():
        # File-sync delete tombstones: reap past their 30-day TTL (an
        # offline satellite is assumed long-since caught up). Indexed delete.
        from storage.files import file_tombstones_store as _tstore
        await _run_db(_tstore.delete_expired)

    async def _sweep_idle_fingerprints():
        # Fingerprint-gated periodic idle sync — for each connected
        # satellite whose agent tree changed OUT-OF-TURN (no active session),
        # run the merge so it (and the dashboard) catches up without waiting
        # for the next session. No-op for quiet / pre-0.5.32 satellites.
        from core.session.session_manager import _get_remote_layer as _grl
        _layer = _grl()
        if _layer is not None:
            await _layer.run_idle_fingerprint_sweep()

    async def _sweep_retention():
        # Session retention + disk cleanup: ages out LOCAL chats' on-disk
        # session files (admin knob; chats reseed from DB history via
        # #11), reaps orphaned session files + Codex telemetry junk, and
        # runs the MCP tarball-cache GC. Internally gated to once/24h.
        from services.infra import retention as _retention
        await _retention.maybe_run_daily()

    async def _sweep_quotas():
        # Storage quotas: measure each agent's shared + per-user buckets
        # and fire the 90/95/100% warning notifications (with hysteresis).
        # Cheap when no limit is set (early-returns); uses the kernel quota
        # report when enforcing, else a throttled tree walk.
        from services.infra import quota_monitor as _qm
        await _qm.check_quotas()

    async def _sweep_subscription_health():
        # OAuth grant health: warn owners before a login grant's
        # finite lifetime lapses (72h/24h out) and once when a row
        # flips to expired. Self-throttled to 15 min; dedup stamps
        # persist inside the credential blob.
        from services.infra import subscription_health as _sub_health
        await _sub_health.check_subscription_health()

    async def _sweep_window_alerts():
        # Provider windows: tell an account's owner when a weekly
        # window passes 90 % and when it is reached. Self-throttled
        # to 5 min; dedup rows persist per window instance.
        from services.infra import subscription_window_alerts as _win_alerts
        await _win_alerts.check_window_alerts()

    async def _sweep_mcp_autoupdate():
        # Automatic MCP updates: once a week in a low-traffic window,
        # apply available community-MCP updates (deferring in-use docker
        # MCPs). Persisted wall-clock gate; launches the run as its own
        # task so the multi-hour defer loop never blocks this sweep.
        from services.mcp import mcp_autoupdate as _mcp_autoupdate
        await _mcp_autoupdate.maybe_run_weekly()

    async def _sweep_agent_updates():
        # Community agent templates: once a day, the managers of an
        # agent whose template has a newer catalog version hear it,
        # once per version (the update itself is theirs to press).
        from services.community import community_agent_updater as _agent_updater
        await _agent_updater.maybe_notify_updates()

    async def _sweep_catalog_installs():
        from core.credentials import catalog_install_registry as _catalog_install_registry
        await _catalog_install_registry.sweep_stale()

    sweep_items = [
        ("warmup_registry sweep", _warmup_registry.sweep_stale),
        ("install_registry sweep", _install_registry.sweep_stale),
        ("transfer_registry sweep", _transfer_registry.sweep_stale),
        ("catalog_install_registry sweep", _sweep_catalog_installs),
        ("media cache sweep", _sweep_media),
        ("preview snapshot sweep", _sweep_preview_snapshots),
        ("recover-bin reap", _sweep_recover_bin),
        ("tombstone reap", _sweep_tombstones),
        ("idle fingerprint sweep", _sweep_idle_fingerprints),
        ("retention sweep", _sweep_retention),
        ("quota monitor sweep", _sweep_quotas),
        ("subscription health sweep", _sweep_subscription_health),
        ("subscription window alert sweep", _sweep_window_alerts),
        ("mcp auto-update sweep", _sweep_mcp_autoupdate),
        ("agent template update sweep", _sweep_agent_updates),
    ]

    ticks: set[asyncio.Task] = set()

    async def _registry_sweep_loop():
        # A tick is started, not awaited: an item that hangs is skipped at
        # the next tick and holds nothing of it (each tick has its own
        # slots), instead of holding every later tick back.
        while True:
            await asyncio.sleep(60)
            tick = asyncio.create_task(run_sweep_items(sweep_items))
            ticks.add(tick)
            tick.add_done_callback(ticks.discard)

    _bg_tasks.append(asyncio.create_task(_registry_sweep_loop()))
    logger.info("Warmup + install registry sweeper started (60s interval)")
    from api.media import uploads as _uploads
    _bg_tasks.append(asyncio.create_task(_uploads.staging_sweep_loop()))

    # Hosted turn-classifier token refresh. When the Groq turn classifier runs
    # through the hosted relay, the minted token baked into the pushed phone
    # config expires server-side after 24h, and the proxy re-mints at most every
    # 12h (in-process cache). Re-push the phone config every 6h so a long-lived
    # phone daemon always holds a fresh, non-expired token. Cheap + gated: a
    # no-op unless the hosted classifier is actually in use (an active relay Groq
    # sub with no BYO key overriding it — base_url is empty otherwise), and the
    # broadcast itself no-ops when no phone daemon is connected.
    async def _phone_classifier_token_refresh_loop():
        from services.phone import phone_config
        while True:
            await asyncio.sleep(6 * 3600)
            try:
                _, base_url = phone_config.direct_llm_groq_credentials()
                if base_url:
                    await phone_config.notify_phone_config_changed()
            except Exception:
                logger.exception("phone classifier token refresh failed")

    _bg_tasks.append(asyncio.create_task(_phone_classifier_token_refresh_loop()))
    logger.info("Hosted phone-classifier token refresh started (6h interval)")

    # OAuth token refresh worker — proactively refreshes per-user
    # OAuth tokens whose access lifetime is <5 min remaining, so agents
    # never pay a refresh round-trip on the first call after a quiet
    # period. Started AFTER scan_manifests so the provider
    # registry is populated when the worker first scans.
    from services.oauth import oauth_refresh_worker
    oauth_refresh_worker.start_worker()

    # Webhook subscription renewal worker — extends
    # vendor-side subscriptions before their TTL expires. MS Graph
    # subscriptions cap at 3 days; the worker renews with 24h lead time
    # so even prolonged worker downtime still gives plenty of room.
    # Mirrors the oauth_refresh_worker lifecycle exactly.
    from services.webhooks import subscription_renewer
    subscription_renewer.start_worker()

    # License liveness worker — a connected, self-hosted install with
    # a subscription/lifetime key binds once (activation) then checks in weekly.
    # No-op (doesn't start) when cloud or the relay isn't configured.
    from services.billing import license_check_worker
    license_check_worker.start_worker()

    # Phone-server health + drift worker — probes verified phone servers'
    # adapters and reconciles their provisioned routes against the DB. No-op
    # until a phone server is added.
    from services.phone import phone_health_worker
    phone_health_worker.start_worker()

    # Token-freshness worker — keeps every bound OAuth subscription's runway
    # above the turn-guard threshold, fanning each rotation out to live
    # sessions' credential files in place, so no session (interactive
    # terminals and otodock-attached ones included) ever reaches its token's
    # death. Also captures the event loop the fan-out uses for satellite
    # credential pushes.
    from services.engines import token_fanout
    token_fanout.start_worker()

    # The first request that falls through every route would otherwise pay
    # FastAPI's lazy route-state build on the loop; pay it now.
    try:
        warm_routes(app)
    except Exception:
        logger.exception("route warm-up failed (non-fatal)")

    # From here on, store calls made ON the loop thread use its private pool
    # (storage/pg.py LoopPool): a Postgres restart or freeze then costs the
    # loop one short wait, not the pool's timeout per call. Every boot step
    # above used the shared pool with its normal wait.
    pg_pool.arm_loop_pool()

    # Event-loop stall watchdog — deliberately the LAST boot step: every
    # synchronous boot step above (schema init, preflight, manifest scan,
    # concurrency init) has run, so a long boot can never read as a stall.
    # Stopped in _shutdown_cleanup right before the pool closes.
    from core import loop_watchdog
    loop_watchdog.watch_executor("default", default_executor)
    for _lane in (pg_pool.LANE_FAST, pg_pool.LANE_BULK):
        loop_watchdog.watch_executor(f"run_db-{_lane}", pg_pool.db_executor(_lane))
    if loop_watchdog.start(config.LOOP_WATCHDOG_THRESHOLD_S, config.LOOP_WATCHDOG_REPORT_S):
        logger.info(
            "Event-loop watchdog started (threshold %.1fs, slow-loop reports from %.2fs)",
            config.LOOP_WATCHDOG_THRESHOLD_S, config.LOOP_WATCHDOG_REPORT_S,
        )

    yield
    # ── Graceful Shutdown ──
    logger.info("Proxy shutdown starting...")

    # Cancel the refresh worker BEFORE _shutdown_sessions so a refresh
    # never lands mid-drain.
    try:
        await oauth_refresh_worker.stop_worker()
    except Exception:
        logger.exception("OAuth refresh worker shutdown failed (continuing)")

    try:
        await subscription_renewer.stop_worker()
    except Exception:
        logger.exception("Subscription renewer shutdown failed (continuing)")

    try:
        await license_check_worker.stop_worker()
    except Exception:
        logger.exception("License check worker shutdown failed (continuing)")

    try:
        await phone_health_worker.stop_worker()
    except Exception:
        logger.exception("Phone health worker shutdown failed (continuing)")

    try:
        await token_fanout.stop_worker()
    except Exception:
        logger.exception("Token-freshness worker shutdown failed (continuing)")

    try:
        await asyncio.wait_for(_shutdown_sessions(logger), timeout=30)
    except (asyncio.TimeoutError, TimeoutError):
        logger.warning("Shutdown timed out after 30s — force-closing remaining resources")

    # App servers: a TERM each (they die with the proxy anyway; this lets
    # them flush).
    try:
        from services.apps import app_supervisor as _apps
        await asyncio.wait_for(_apps.stop_all(), timeout=15)
    except Exception:
        logger.exception("App supervisor shutdown failed (continuing)")

    # Close the HTTP tunnel dispatcher (httpx client + sweeper task).
    try:
        from core.remote.satellite_http_tunnel import get_dispatcher
        await get_dispatcher().shutdown()
    except Exception:
        logger.exception("HTTP tunnel dispatcher shutdown failed (continuing)")

    await _shutdown_cleanup(logger)
    logger.info("Proxy shutdown complete")


async def _flush_active_pumps(logger) -> None:
    """Flush still-open pumps' turn text before the process exits — the
    lost-transcript class. Mode C-recoverable chats are SKIPPED: the satellite
    replays their whole retained turn through a fresh recovery pump after the
    restart, so a shutdown-time abort would stamp a stale interrupted marker
    and duplicate the replayed rows."""
    from services.scheduler import run_recovery
    pumps = list(_active_pumps.items())
    if not pumps:
        return
    from core.events.stream_pump import suppress_recovery_flush
    from core.session import session_kind
    from core.session.session_state import mark_recover_pending
    waits = []
    for chat_id, pump in pumps:
        try:
            if run_recovery.is_recovery_eligible(chat_id):
                # Replayed after the restart whether it is still running or
                # finished meanwhile: nothing of it is written now. A run is
                # parked and recovered by its run id instead.
                suppress_recovery_flush(chat_id)
                if pump.source_type != session_kind.TASK.source_type:
                    mark_recover_pending(pump.session_id)
                continue
            pump.abort()
            task = getattr(pump, "_task", None)
            if task is not None:
                waits.append(task)
        except Exception as e:
            logger.warning(f"Shutdown: pump flush {chat_id[:8]} error: {e}")
    if waits:
        with contextlib.suppress(Exception):
            await asyncio.wait(waits, timeout=5)
        logger.info(f"Shutdown: flushed {len(waits)} open pump(s)")


def _arm_exit_failsafe() -> None:
    """Guarantee process exit after clean shutdown. A clean exit beats the
    timer (a daemon timer dies with the process); a blocked non-daemon thread
    makes it fire — log + hard-exit with a DISTINCT code so the journal
    distinguishes the failsafe from a clean stop."""
    if not _ARM_EXIT_FAILSAFE:
        return

    def _fire() -> None:
        from core import log_queue
        logger.error(
            "Shutdown failsafe fired after %.0fs — a non-daemon thread "
            "blocked interpreter exit (SIGUSR1 dumps stacks next time); "
            "hard-exiting with code 3", _EXIT_FAILSAFE_GRACE_S,
        )
        # A log writer that cannot drain in 2 s is wedged on the disk and
        # holds the file handler's lock — logging.shutdown() would block on
        # it and the exit below would never happen.
        if log_queue.drain(2.0):
            logging.shutdown()
        os._exit(3)

    timer = threading.Timer(_EXIT_FAILSAFE_GRACE_S, _fire)
    timer.daemon = True
    timer.start()


async def _shutdown_cleanup(logger) -> None:
    """The ordered shutdown tail. Order is load-bearing: everything that
    writes the DB (pump flush, orphan cleanup) runs BEFORE the pool closes;
    the failsafe arms LAST so a clean exit beats it."""
    await _flush_active_pumps(logger)

    # Safety net: mark any still-active DB records as failed.
    # Handles both close failures and non-graceful prior crashes — but LEAVE
    # recovery-eligible remote runs running (their CLI is still alive on the
    # satellite; the next boot parks + re-adopts them). They're recognised by
    # the same eligibility check the CancelledError handler used.
    try:
        from services.scheduler import run_recovery
        keep = [
            r["id"] for r in task_store.list_orphaned_runs()
            if run_recovery.is_recovery_eligible(r.get("chat_id") or "")
        ]
        orphaned_runs = task_store.mark_orphaned_runs_failed(exclude_ids=keep)
        orphaned_meetings = task_store.mark_orphaned_meetings_failed()
        if orphaned_runs or orphaned_meetings or keep:
            logger.info(
                f"Shutdown cleanup: marked {orphaned_runs} run(s) and "
                f"{orphaned_meetings} meeting(s) as failed; "
                f"left {len(keep)} recoverable run(s) running"
            )
    except Exception as e:
        logger.error(f"Shutdown DB cleanup failed: {e}")

    # Cancel the lifespan's own background loops so their teardown doesn't
    # race the closed pool below (asyncio.run would cancel them anyway —
    # but only AFTER lifespan, with the pool already gone).
    for task in _bg_tasks:
        task.cancel()
    if _bg_tasks:
        with contextlib.suppress(Exception):
            await asyncio.wait(_bg_tasks, timeout=3)
    _bg_tasks.clear()

    scheduler.stop()

    # Satellite status/caps persists queued by the WS teardown (uvicorn
    # closes sockets BEFORE lifespan shutdown) are still in flight on the DB
    # executor — give them a bounded moment to land, then stop the executor
    # (cancel what never started; a worker parked on a stalled commit is the
    # exit failsafe's problem, not ours).
    try:
        from core.remote.satellite_connection import get_connection_manager
        if not await get_connection_manager().drain_persists(timeout=2.0):
            logger.warning("Shutdown: satellite status persists still pending after 2s")
    except Exception:
        logger.exception("Shutdown: persist drain failed (continuing)")
    # Same for the per-chat writer lanes (turn rows queued by pumps that were
    # still unwinding when the sockets closed).
    try:
        from core.events import chat_writer
        if not await chat_writer.drain_all(timeout=3.0):
            logger.warning("Shutdown: chat writer jobs still pending after 3s")
    except Exception:
        logger.exception("Shutdown: chat writer drain failed (continuing)")
    # The session index is written behind; its last changes land now, on
    # this thread (a write must not depend on a free executor thread here).
    try:
        from core.session import session_state
        session_state.flush_session_index()
    except Exception:
        logger.exception("Shutdown: session index flush failed (continuing)")
    # The watchdog reported stalls through the DB work above; stop it now
    # (flag first, then the tick task — never a fake stall at shutdown).
    from core import loop_watchdog
    loop_watchdog.stop()
    # The binding writer's queued seat releases land before the pools close,
    # drained in a worker thread with a bound (30 s: a stalled database drops
    # what is still queued, which the next boot's counter reset repairs); a
    # write after this runs inline.
    from services.engines import subscription_pool as _subscription_pool
    try:
        await asyncio.wait_for(
            asyncio.to_thread(_subscription_pool.close_binding_writes),
            _subscription_pool._BINDING_DRAIN_TIMEOUT_S + 5,
        )
    except (asyncio.TimeoutError, TimeoutError):
        logger.warning("Shutdown: the binding writer did not close in time (continuing)")
    except Exception:
        logger.exception("Shutdown: binding writer close failed (continuing)")
    pg_pool.shutdown_db_executor()

    # From here down nothing may touch the DB.
    pg_pool.close_pool()

    from core.layers.direct.mcp import stop_mcp_thread
    stop_mcp_thread()

    _arm_exit_failsafe()


async def _shutdown_sessions(logger):
    """Close all active sessions across all layers (called with timeout)."""
    # 1. Cancel running tasks and meetings first (they depend on sessions)
    await scheduler.shutdown()
    from services.meetings.meeting_orchestrator import shutdown_meetings
    await shutdown_meetings()

    # 2. Close all sessions.
    # Every local engine, layer by layer, straight from the registry: the
    # layer that HOLDS a session is the one that closes it, so shutdown needs
    # neither the three private pool dicts nor the agent row (which it only
    # ever read to work out which layer to ask — and which is gone for an
    # agent deleted while its session ran, silently skipping the close).
    from core.session.session_manager import get_all_layers

    for path, layer in get_all_layers().items():
        for sid in layer.local_session_ids():
            try:
                await layer.close_session(sid)
            except Exception as e:
                logger.warning(f"Shutdown: {path} {sid[:8]} error: {e}")

    from core.session.session_manager import _remote_layer
    if _remote_layer:
        await _shutdown_remote_sessions(_remote_layer, logger)


async def _shutdown_remote_sessions(remote_layer, logger) -> None:
    """Remote sessions at shutdown. One whose engine takes a session back
    after a restart (``readopts_idle_session``: Claude and Codex) is LEFT
    OPEN, idle or mid-turn, with its security context: the satellite keeps
    its process and reports it, and the new process re-adopts it (a Claude
    turn in flight is replayed; a Codex one, with no replay, is closed by
    that report). Closing it here could not reach the satellite anyway
    (uvicorn closes the sockets before the lifespan's shutdown) and would
    only drop the context, so the session came back with every hook refused.
    Other engines close as before."""
    from core.session.session_manager import get_layer_capabilities
    for sid in remote_layer.local_session_ids():
        info = remote_layer._sessions.get(sid)
        caps = get_layer_capabilities(getattr(info, "execution_path", "") or "") if info else None
        if caps is not None and caps.runtime.readopts_idle_session:
            logger.info(
                f"Shutdown: leaving remote {sid[:8]} open for re-adoption "
                f"({'mid-turn' if getattr(info, 'turn_active', False) else 'idle'})"
            )
            continue
        try:
            await remote_layer.close_session(sid)
        except Exception as e:
            logger.warning(f"Shutdown: Remote {sid[:8]} error: {e}")
