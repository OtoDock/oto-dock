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
            if system:
                pump.system_queue.append(text)
            else:
                pump.queue_message(text)
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
    from core.sandbox.sandbox import netns_preflight, cli_version_preflight
    netns_preflight()

    # CLI pin drift check (warn-only): if the proxy host's claude/codex differ
    # from VERSIONS.md, log it so an operator can reconcile. Satellites self-heal
    # on auth; the proxy host is fixed by re-running the installer.
    cli_version_preflight()

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

    # Initialize PostgreSQL schema (all tables + migrations)
    with pg_pool.get_conn() as _pg_conn:
        pg_schema.init_schema(_pg_conn)
        pg_schema.run_migrations(_pg_conn)
        _pg_conn.commit()
        # Drift guard: one loud ERROR when the live DB is missing columns the
        # code expects (a CREATE TABLE-only addition never reaches existing
        # installs) instead of scattered UndefinedColumn 500s at query time.
        pg_schema.check_schema_drift(_pg_conn)
    # Credential-key canary: one loud ERROR when stored secrets can't be
    # decrypted (JWT_SECRET changed / config.env recreated) instead of
    # scattered use-time 500s and silently-empty credential reads.
    from storage.credential_store import startup_key_canary
    startup_key_canary()
    # First-install only: default the platform timezone to the server's local
    # wall clock (UTC otherwise). Guarded on the setting being absent → set once,
    # never overwritten by later updates or an admin's change.
    from storage import database as _db_seed
    _db_seed.seed_platform_timezone_if_unset()
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

    from storage import agent_store
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
    # Bootstrap core-MCP assignments for pre-existing agents.
    # ``api/agents/agents.py:create_agent`` auto-assigns core MCPs on agent
    # creation, but adding a new core MCP later (e.g. memory-mcp on
    # platform upgrade) needs a one-shot backfill. Idempotent — the
    # underlying ``ON CONFLICT DO NOTHING`` insert means re-running is
    # cheap.
    try:
        from storage import mcp_store
        for _manifest_name, _manifest in mcp_registry.get_all_manifests().items():
            if _manifest.category != "core":
                continue
            if _manifest.assignment_mode == "explicit":
                continue
            for _agent in agent_store.get_all_agents():
                mcp_store.add_agent_mcp(_agent["slug"], _manifest_name)
                # Also ensure skill rows so they auto-load.
                for _skill in _manifest.skills:
                    mcp_store.ensure_agent_skill(
                        _agent["slug"], _skill.id,
                        default_enabled=True,
                        default_exclude_from=_skill.default_exclude_from,
                    )
    except Exception:
        logger.exception("core-MCP bootstrap-assign failed (non-fatal)")
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
    from storage import subscription_store
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

    async def _registry_sweep_loop():
        while True:
            await asyncio.sleep(60)
            try:
                await _warmup_registry.sweep_stale()
            except Exception:
                logger.exception("warmup_registry sweep failed")
            try:
                await _install_registry.sweep_stale()
            except Exception:
                logger.exception("install_registry sweep failed")
            try:
                from core.credentials import catalog_install_registry as _catalog_install_registry
                await _catalog_install_registry.sweep_stale()
            except Exception:
                logger.exception("catalog_install_registry sweep failed")
            try:
                from services.media import media_pipeline as _mp
                from storage import database as _db
                _mp.sweep_host_media_cache()      # TTL satellite-host media
                _db.sweep_expired_media_tokens()  # reap expired workspace tokens
            except Exception:
                logger.exception("media cache sweep failed")
            try:
                # Version-pinned preview snapshots: reap dirs of deleted chats
                # (internally throttled to hourly).
                from services.media import preview_snapshots as _psnap
                _psnap.sweep_orphans()
            except Exception:
                logger.exception("preview snapshot sweep failed")
            try:
                # Workspace Recover Bin: reap entries past their 7-day TTL
                # (DB rows + on-disk bytes). Quick indexed delete, usually 0.
                from storage import recover_bin_store as _rbstore
                _rbstore.delete_expired()
            except Exception:
                logger.exception("recover-bin reap failed")
            try:
                # File-sync delete tombstones: reap past their 30-day TTL (an
                # offline satellite is assumed long-since caught up). Indexed delete.
                from storage import file_tombstones_store as _tstore
                _tstore.delete_expired()
            except Exception:
                logger.exception("tombstone reap failed")
            try:
                # Fingerprint-gated periodic idle sync — for each connected
                # satellite whose agent tree changed OUT-OF-TURN (no active session),
                # run the merge so it (and the dashboard) catches up without waiting
                # for the next session. No-op for quiet / pre-0.5.32 satellites.
                from core.session.session_manager import _get_remote_layer as _grl
                _layer = _grl()
                if _layer is not None:
                    await _layer.run_idle_fingerprint_sweep()
            except Exception:
                logger.exception("idle fingerprint sweep failed")
            try:
                # Session retention + disk cleanup: ages out LOCAL chats' on-disk
                # session files (admin knob; chats reseed from DB history via
                # #11), reaps orphaned session files + Codex telemetry junk, and
                # runs the MCP tarball-cache GC. Internally gated to once/24h.
                from services.infra import retention as _retention
                await _retention.maybe_run_daily()
            except Exception:
                logger.exception("retention sweep failed")
            try:
                # Storage quotas: measure each agent's shared + per-user buckets
                # and fire the 90/95/100% warning notifications (with hysteresis).
                # Cheap when no limit is set (early-returns); uses the kernel quota
                # report when enforcing, else a throttled tree walk.
                from services.infra import quota_monitor as _qm
                await _qm.check_quotas()
            except Exception:
                logger.exception("quota monitor sweep failed")
            try:
                # Automatic MCP updates: once a week in a low-traffic window,
                # apply available community-MCP updates (deferring in-use docker
                # MCPs). Persisted wall-clock gate; launches the run as its own
                # task so the multi-hour defer loop never blocks this sweep.
                from services.mcp import mcp_autoupdate as _mcp_autoupdate
                await _mcp_autoupdate.maybe_run_weekly()
            except Exception:
                logger.exception("mcp auto-update sweep failed")

    _bg_tasks.append(asyncio.create_task(_registry_sweep_loop()))
    logger.info("Warmup + install registry sweeper started (60s interval)")

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

    yield
    # ── Graceful Shutdown ──
    logger.info("Proxy shutdown starting...")

    # Cancel the refresh worker BEFORE _shutdown_sessions so it doesn't
    # fight credential_writeback for per-account locks during drain.
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
    waits = []
    for chat_id, pump in pumps:
        try:
            if run_recovery.is_recovery_eligible(chat_id):
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
        logger.error(
            "Shutdown failsafe fired after %.0fs — a non-daemon thread "
            "blocked interpreter exit (SIGUSR1 dumps stacks next time); "
            "hard-exiting with code 3", _EXIT_FAILSAFE_GRACE_S,
        )
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

    # 2. Close all sessions
    from core.layers.cli.session import _persistent_sessions, _persistent_sessions_lock
    from core.layers.direct.session import _direct_sessions, _direct_sessions_lock
    from core.layers.codex.session import _codex_sessions, _codex_sessions_lock
    from core.session.session_manager import get_execution_layer
    from core.session.session_state import _sessions

    # CLI sessions
    async with _persistent_sessions_lock:
        cli_sids = list(_persistent_sessions.keys())
    for sid in cli_sids:
        try:
            agent = _sessions.get(sid, {}).get("agent", "")
            if agent:
                await get_execution_layer(agent).close_session(sid)
        except Exception as e:
            logger.warning(f"Shutdown: CLI {sid[:8]} error: {e}")

    # Direct LLM sessions
    async with _direct_sessions_lock:
        direct_sids = list(_direct_sessions.keys())
    for sid in direct_sids:
        try:
            agent = _sessions.get(sid, {}).get("agent", "")
            if agent:
                await get_execution_layer(agent, execution_path="direct-llm").close_session(sid)
        except Exception as e:
            logger.warning(f"Shutdown: Direct {sid[:8]} error: {e}")

    # Codex sessions
    async with _codex_sessions_lock:
        codex_sids = list(_codex_sessions.keys())
    for sid in codex_sids:
        try:
            agent = _sessions.get(sid, {}).get("agent", "")
            if agent:
                await get_execution_layer(agent, execution_path="codex-cli").close_session(sid)
        except Exception as e:
            logger.warning(f"Shutdown: Codex {sid[:8]} error: {e}")

    # Remote sessions. A remote CLI session with an in-flight turn is LEFT
    # OPEN so the satellite keeps its CLI alive for Mode C re-adopt after the
    # restart — closing it would send close_session → kill the CLI → nothing
    # to recover. Idle/Codex/direct remote sessions close normally.
    from core.session.session_manager import _remote_layer
    if _remote_layer:
        remote_sids = list(_remote_layer._sessions.keys())
        for sid in remote_sids:
            _info = _remote_layer._sessions.get(sid)
            if (_info is not None
                    and _info.execution_path == "claude-code-cli"
                    and getattr(_info, "turn_active", False)):
                logger.info(
                    f"Shutdown: leaving in-flight remote CLI {sid[:8]} open "
                    f"for satellite re-adopt"
                )
                continue
            try:
                await _remote_layer.close_session(sid)
            except Exception as e:
                logger.warning(f"Shutdown: Remote {sid[:8]} error: {e}")
