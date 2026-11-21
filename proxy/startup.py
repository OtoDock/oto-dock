"""Application lifespan: startup orchestration + graceful shutdown.

Extracted from ``app.py`` — ``app = FastAPI(lifespan=lifespan)`` wires this in.
Boots the sandbox/quotas preflights, DB schema, background workers, reapers and
sweepers on startup, and drains sessions + workers on shutdown.
"""

import asyncio
import logging
import os
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
from typing import cast

from fastapi import FastAPI

import config
from core.events.stream_pump import _active_pumps
from core.layers.direct import reap_idle_direct_sessions
from core.session.session_state import set_pump_callbacks
from services.notifications import notification_manager
from services.scheduler import scheduler
from startup_shutdown import (  # noqa: F401
    _arm_exit_failsafe,
    _bg_tasks,
    _flush_active_pumps,
    _shutdown_cleanup,
    _shutdown_remote_sessions,
    _shutdown_sessions,
    _stop_workers,
)
from startup_sweeps import (  # noqa: F401
    _purge_mcp_build_outputs,
    _registry_sweep_loop,
    _sweep_items,
    _sweep_ticks,
    run_sweep_items,
)
from storage import database as task_store, pg as pg_pool, schema as pg_schema

logger = logging.getLogger("otodock")

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
            except Exception:  # noqa: BLE001  # Starlette's matcher on a synthetic scope may raise anything
                logger.debug("route warm-up skipped %r", route, exc_info=True)


# Register pump callbacks so scheduler / session_state can push events
# and queue prompts on active pumps without circular imports.
def _push_to_pump(chat_id: str, event: dict) -> bool:
    pump = _active_pumps.get(chat_id)
    if pump:
        return pump.push_ws_event(event)
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
        pump.event_queue.put_nowait(event)
        return True
    return False


# Docker MCP bring-up runs in the BACKGROUND: on a fresh box it pulls
# multi-GB images (file-tools → Collabora), and doing that synchronously
# here blocked the HTTP bind for minutes with the service reading as
# `active` but unbound — indistinguishable from a hang. Sessions that
# start before a container is up get connect-refused tool errors until it
# is (same behavior as a boot where docker was unavailable); the pull
# progress itself is logged by docker_manager.
async def _docker_mcp_bringup(docker_manager) -> None:
    """The docker MCPs brought up off the loop, a failure logged. The server
    sessions the tools' door held before this start are lost (their
    sessions' next calls say so)."""
    from services.containers import door
    door.load_held()
    try:
        await asyncio.to_thread(docker_manager.startup_docker_mcps)
    except Exception:
        logger.exception("Docker MCP bring-up failed (non-fatal)")


# The language identifier (dictation's Automatic pick): the 22 MB model
# (about 100 MB resident) loads once per process, off the loop, ahead of
# the first socket (one that lands first loads it itself, off the loop),
# and only where a chat STT provider exists.
def _prewarm_language_identifier() -> None:
    from services.media import audio_service
    if audio_service.language_identify_prewarm_wanted():
        from audio.providers.lid.ecapa import prewarm_language_identifier
        prewarm_language_identifier(None, audio_service.language_identify_threads())


# What the last process left, once the boot settles, in this order: the
# messages that waited in a chat's queue are armed, the turns it cut are
# continued (``continue_pass``), the reloads and moves that waited are
# settled, the runs that never started start, the wakes parked on chats are
# redelivered (delayed so satellites get their reconnect window first: a
# wake whose machine is still offline just re-persists and that machine's
# own reconnect pass retries it), and what came due in the downtime fires.
# The order is the rule: a wake or a fire must not start a session on a chat
# whose cut turn the pass has not reached.
_BOOT_SETTLE_S = 10.0
_BOOT_WAKES_AFTER_S = 15.0


async def _end_start_hold() -> None:
    """Wait for a compute machine the reloaded sessions run on
    (``compute_bootstrap.await_back``, bounded), then end the start's hold.
    The continue's steps run after this, so what a door held goes out in the
    boot's order."""
    from core import drain
    from services.remote import compute_bootstrap
    if not drain.starting():
        return
    try:
        await compute_bootstrap.await_back()
    except BaseException:
        drain.drop_start_hold()
        raise
    await drain.end_start_hold()


async def _boot_continue() -> None:
    from services.apps import link_upgrade
    from services.mcp import mcp_venv_bootstrap, tool_listing
    from services.phone import outside_upgrade, wrapup
    from services.scheduler import continue_pass, run_recovery
    await asyncio.sleep(_BOOT_SETTLE_S)
    await _end_start_hold()
    steps = (
        ("the queued messages", continue_pass.arm_queues),
        ("the continue pass", continue_pass.run_pending),
        ("the runs not taken up again", run_recovery.report_failed_at_boot),
        ("the waiting reloads and moves", continue_pass.settle_waiters),
        ("the runs that never started", run_recovery.start_pending_runs),
        ("the outside callers of the update", outside_upgrade.first_boot),
        ("the earlier calls of the update", outside_upgrade.name_old_calls),
        ("the conversations tool of the update", outside_upgrade.add_conversations_tool),
        ("the shared links of the update", link_upgrade.first_boot),
        ("the wrap-ups left open", wrapup.resume_after_boot),
        ("the bundled tools of the update", tool_listing.relist_bundled_after_upgrade),
        ("the tools whose environment is not built", mcp_venv_bootstrap.tell_admins),
    )
    for name, step in steps:
        try:
            done = await step()
            if done:
                logger.info("Startup, %s: %s", name, done)
        except Exception:
            logger.exception("Startup, %s: failed (continuing)", name)
    await asyncio.sleep(max(0.0, _BOOT_WAKES_AFTER_S - _BOOT_SETTLE_S))
    try:
        held = await scheduler.fire_held()
        if held:
            logger.info("Startup: fired %d continuation(s) held while the platform started", held)
    except Exception:
        logger.exception("Startup: the held continuations failed")
    try:
        woken = await scheduler.redeliver_pending_wakes()
        if woken:
            logger.info("Startup wake redelivery: woke %d chat(s)", woken)
    except Exception:
        logger.exception("Startup wake redelivery sweep failed")
    try:
        fired = await scheduler.fire_downtime()
        if fired:
            logger.info("Startup: fired %d task(s) that came due while the platform was down",
                        fired)
    except Exception:
        logger.exception("Startup: the downtime's fires failed")


async def _alive_beat() -> None:
    """Say the platform is alive (``platform_marks``), so the next boot
    knows since when it was down."""
    from services.infra import platform_marks
    while True:
        await asyncio.sleep(platform_marks.BEAT_S)
        try:
            await asyncio.to_thread(platform_marks.beat)
        except Exception:  # noqa: BLE001  # a database outage: the next beat tries again
            logger.debug("the alive beat failed", exc_info=True)


def _note_stop_signal() -> None:
    """The stop signal sets the platform's ending before uvicorn's own
    handler runs: an engine that dies while the sockets drain (a unit that
    signals the whole group) is then read as the platform's cut, and the
    chats' queues freeze. Main thread only (``signal.signal``'s rule)."""
    import signal
    import threading

    from core.events.stream_pump import note_platform_ending
    if threading.current_thread() is not threading.main_thread():
        return
    for signum in (signal.SIGTERM, signal.SIGINT):
        previous = signal.getsignal(signum)
        if not callable(previous):
            continue

        def _handler(sig, frame, _previous=previous) -> None:
            note_platform_ending()
            _previous(sig, frame)

        signal.signal(signum, _handler)


# One register per relay provider in use, once boot settles: an upgraded
# install sends its event kinds and signs, a mistaken switch-off is healed,
# and the unbinds left pending are sent.
async def _boot_relay_events() -> None:
    await asyncio.sleep(20)
    try:
        from services.webhooks import relay_events
        await relay_events.sync_all()
    except Exception:
        logger.exception("Startup relay events registration failed (non-fatal)")


# Hosted turn-classifier token refresh. When the Groq turn classifier runs
# through the hosted relay, the minted token baked into the pushed phone
# config expires server-side after 24h, and the proxy re-mints at most every
# 12h (in-process cache). Re-push the phone config every 6h so a long-lived
# phone daemon always holds a fresh, non-expired token. Cheap + gated: a
# no-op unless the hosted classifier is actually in use (an active relay Groq
# sub with no BYO key overriding it — base_url is empty otherwise), and with
# no phone daemon connected nothing is sent (the config is still assembled).
async def _phone_classifier_token_refresh_loop():
    from services.phone import phone_config
    while True:
        await asyncio.sleep(6 * 3600)
        try:
            _, base_url = await phone_config.classifier_credentials()
            if base_url:
                await phone_config.notify_phone_config_changed()
        except Exception:
            logger.exception("phone classifier token refresh failed")


def not_dumpable() -> bool:
    """On bare metal the compute unit runs as the platform's own OS user: the
    server takes itself out of that user's reach through ``/proc`` (its
    environment holds config.env's secrets, its root and its open files lead
    past the unit's walls), as ``prctl(PR_SET_DUMPABLE, 0)`` does. A container
    is a boundary of its own, and an install that runs no compute machine of
    its own keeps its core dumps and an inspector of the same user. True when
    set."""
    import config
    from core import host_os
    from services.remote import compute_bootstrap
    if config.RUNNING_IN_DOCKER or host_os.host_os().name != host_os.LINUX or not compute_bootstrap.runs_own():
        return False
    try:
        import ctypes
        libc = ctypes.CDLL(None, use_errno=True)
        if libc.prctl(4, 0, 0, 0, 0) != 0:  # PR_SET_DUMPABLE
            raise OSError(ctypes.get_errno(), "prctl")
    except (OSError, AttributeError):
        logger.warning("Startup: the server could not take itself out of reach of /proc", exc_info=True)
        return False
    return True


def _boot_process() -> ThreadPoolExecutor:
    """The process: the SIGUSR1 dump, the nofile limit, the default executor,
    the build outputs purged, the adapters, the pump callbacks, the agents
    directory, the storage quotas."""
    # SIGUSR1 → all-thread stack dump on stderr; the field tool for shutdown
    # hangs ("Finished server process" logged but the process never exits).
    try:
        import faulthandler
        import signal
        faulthandler.register(signal.SIGUSR1, all_threads=True)
    except (ValueError, AttributeError, OSError):
        pass  # non-main thread or no SIGUSR1 on this platform

    raise_nofile_limit()
    not_dumpable()
    default_executor = set_default_executor(asyncio.get_running_loop())
    _purge_mcp_build_outputs()

    # Startup — register client adapters
    from adapters import register_adapter
    from adapters.dashboard import DashboardAdapter
    from adapters.phone import PhoneAdapter
    register_adapter(PhoneAdapter())
    register_adapter(DashboardAdapter())

    set_pump_callbacks(_push_to_pump, _queue_on_pump, _inject_to_pump)

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
    return default_executor


async def _boot_database() -> None:
    """The database: the schema and its migrations, the version guard, the
    network self-check, the credential sweep."""
    # Initialize PostgreSQL schema (all tables + migrations). One
    # transaction; its DDL may wait on locks a running pg_dump holds, so the
    # pool's statement timeout does not apply to it, and it waits longer for
    # a connection than a request would (Postgres may still be starting). A
    # database a NEWER release migrated refuses the boot here
    # (schema.platform_version_preflight), before any DDL of this release.
    with pg_pool.get_conn(timeout=60) as _pg_conn:
        _pg_conn.execute("SET LOCAL statement_timeout = 0")
        from services.engines import engine_resolution
        pg_schema.migrate_database(
            _pg_conn, config.PINNED_OTODOCK_VERSION,
            allow_newer=cast("bool", config.ALLOW_NEWER_DATABASE),  # default False: never None
            engines=engine_resolution.engine_order())
        _pg_conn.commit()
        # The agent rows a migration rewrote (the allowlist widening) must
        # not be served from a cache filled before the commit.
        from storage.agents import agent_store
        agent_store._invalidate_cache()
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


#: Why each key of ``config.UNREAD_KEYS_SET`` does nothing now, said once at
#: the boot.
UNREAD_KEY_SENTENCES = {
    "OTODOCK_COMPUTE_SESSIONS": "chats, tasks and calls run on a compute machine.",
    "OTODOCK_CREDENTIAL_EXIT": "the credential exit always runs.",
}


def _boot_compute() -> None:
    """The install's own compute machine: its row and its credential, minted
    again when they do not verify (a restore onto a new host), and the
    start's hold until it signed in. The platform runs no session itself, so
    a start that has no compute machine to wait for says so: no silent
    start."""
    from core import drain
    from services.remote import compute_bootstrap
    for key in config.UNREAD_KEYS_SET:
        logger.warning("Config: %s is set and does nothing since 2.0: %s Delete the line.", key,
                       UNREAD_KEY_SENTENCES.get(key, ""))
    if not compute_bootstrap.runs_own():
        logger.warning("Compute: this install names no compute machine of its own "
                       "(OTODOCK_COMPUTE_LINK_DIR is not set). Chats, tasks and calls cannot run until one signs in: "
                       "use the compose file's otodock-compute service, or on an install from "
                       "source run scripts/dev-setup.sh --service.")
    try:
        compute_bootstrap.ensure()
    except Exception:
        logger.exception("Compute: the compute machine's credential was not written. It cannot "
                         "sign in, so chats, tasks and calls cannot run until this is fixed (continuing)")
    # The addresses the compute machines on other servers signed in from: never
    # the local network a person signs in from (``lan_check``).
    try:
        compute_bootstrap.load_addresses()
    except Exception:
        logger.exception("Compute: the compute machines' addresses were not read (continuing)")
    if compute_bootstrap.expect_back():
        drain.hold_for_start()


async def _boot_registrations() -> None:
    """The registrations and the one-time passes: offboarding, the app
    lifecycle, the key canary, the timezone seed, the edge recompile, the
    run recovery, the security index, the revocations, the histories'
    move, the persona migration, the app releases, the catalog, the checks, the seeder, the
    app handlers, the license and phone seeds."""
    # The stores' post-commit steps (an agent's repo and quota, a person's
    # folders): installed before anything below can create an agent or
    # change a membership.
    from services.agents import agent_provisioning
    agent_provisioning.install()
    # Offboarding: a person's live sessions close with their access, their
    # automations follow the admin who took it away, and the service
    # bindings they lent follow their standing.
    from services.agents import (
        offboarding_bindings,
        offboarding_sessions,
        offboarding_transfer,
    )
    offboarding_sessions.register()
    offboarding_transfer.register()
    offboarding_bindings.register()
    from ws import dashboard as _dashboard_ws
    _dashboard_ws.register_offboarding()
    from api.media import wopi as _wopi
    _wopi.register_offboarding()
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
    # A department set's install the last run was cut off in is marked so:
    # its agents stand alone, the person installs the set again.
    try:
        from services.community import department_set_installer as _set_installer
        for job_id in _set_installer.interrupt_at_boot():
            logger.warning("set install %s was cut off by the restart", job_id)
    except Exception:
        logger.exception("startup set-install interrupt failed (non-fatal)")
    # What the last process left (``platform_marks``): whether the boot takes
    # up what it finds, and how the last process stopped. Stamped below, once
    # the answer is in the store.
    from services.infra import platform_marks
    marks = platform_marks.read_at_boot()
    # Startup recovery: reload the persisted per-session SecurityContext
    # index so a session that survived a proxy crash on a satellite (and its
    # background sub-agents) keeps full path-policy enforcement at the permission
    # gate instead of being denied as context-less. Contexts carry no secrets;
    # closed sessions were cleared, so a replayed dead-session JWT stays denied.
    # Before the records below: recovery reads a session's machine from its
    # own context (``session_state.placed_on``), never from its chat's pin.
    from core.session.session_state import load_session_security
    load_session_security()
    _boot_compute()
    # The turns the last process cut become records (``cut_turns``): one the
    # platform ran itself is the continue pass's once the boot settles, one
    # on a machine waits for that machine's report.
    from services.scheduler import cut_turns
    cut = cut_turns.record_at_boot(clean_stop=marks.clean_stop, catches_up=marks.catches_up)
    if cut["pending"] or cut["waiting"]:
        logger.info("Startup recovery: %s cut turn(s) to continue, %s waiting for their machine",
                    cut["pending"], cut["waiting"])
    # Startup recovery: PARK recovery-eligible in-flight runs (remote CLI,
    # pinned target) for satellite re-adopt (Mode C), keep the runs that are
    # reopened, and fail every other orphaned run and all orphaned meetings
    # so they don't appear stuck. Must run BEFORE the satellite heartbeat
    # monitor (below) so a run is parked before its satellite can report it.
    from services.scheduler import run_recovery
    parked_runs, orphaned_runs = run_recovery.defer_orphaned_runs(
        catches_up=marks.catches_up, clean_stop=marks.clean_stop)
    platform_marks.stamp_at_boot()
    orphaned_meetings = task_store.mark_orphaned_meetings_failed()
    if parked_runs or orphaned_runs or orphaned_meetings:
        logger.info(
            "Startup recovery: parked %s run(s) for re-adopt, "
            "marked %s orphaned run(s) and "
            "%s orphaned meeting(s) as failed",
            parked_runs, orphaned_runs, orphaned_meetings
        )
    # The sign-ins a logout revoked survive a restart (auth/session_revocation.py).
    from auth import session_revocation
    await asyncio.to_thread(session_revocation.load)

    # The histories an earlier release kept inside the tracked folders move
    # into the proxy's own data, in the background: a folder's first use
    # moves its own first (services/infra/history_move.py).
    from services.infra import history_move
    _bg_tasks.append(asyncio.create_task(asyncio.to_thread(history_move.run_pass)))

    # Converge pre-1.4 agents on the agent.md persona filename before any
    # session builds a prompt (readers accept both names; this makes disk,
    # git and satellite sync agree on the new one).
    migrated = await asyncio.to_thread(agent_provisioning.migrate_persona_filenames)
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
        from services.apps import catalog as _catalog
        _catalog.install()
    except Exception:
        logger.exception("Platform catalog install failed (continuing)")
    # A session's own deletes: registered before the
    # checks', whose continue would end the handler loop before it.
    try:
        from services.infra import file_bookkeeping as _bookkeeping
        _bookkeeping.install_session_delete_watch()
        _bg_tasks.append(asyncio.create_task(
            asyncio.to_thread(_bookkeeping.prime_session_delete_watch)))
    except Exception:
        logger.exception("Session delete watch install failed (continuing)")
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
    from services.phone.phone_config_seed import (
        remap_retired_audio_models,
        remap_shipped_call_prompts,
        seed_phone_config,
    )
    seed_phone_config()
    # The audio providers' model ids: a retired id moves to its successor
    # on every boot, a former default is cleared from the rows once (the
    # LLM remap below, applied to audio). Idempotent; must never kill boot.
    try:
        remap_retired_audio_models()
    except Exception:
        logger.exception("audio model remaps failed (non-fatal)")
    # A call prompt no admin ever edited follows the adapter's current text.
    try:
        remap_shipped_call_prompts()
    except Exception:
        logger.exception("call prompt remap failed (non-fatal)")


async def _boot_mcps() -> None:
    """The MCPs: the manifest scan, the bundled venvs, the docker bring-up."""
    # MCP Framework: discover manifests, seed DB, start Docker MCPs
    from services.mcp import docker_manager, mcp_registry, mcp_venv_bootstrap, tool_envs
    mcp_registry.scan_manifests()
    # The browsers whose local network an admin opened: outside people's
    # sessions never get one (``mcp_session_filter``).
    try:
        from services.mcp import mcp_egress, mcp_session_filter
        mcp_session_filter.note_open_exits(await asyncio.to_thread(lambda: [
            m.name for m in mcp_registry.get_all_manifests().values()
            if m.network_exit and mcp_egress.network_access_enabled(m)]))
    except Exception:
        logger.exception("The open browsers were not read (continuing)")
    # The platform's own tools' hosts on the bearer allowlist, as a release
    # declares them (a host a release adds would otherwise wait for nobody).
    try:
        from services.community import manifest_hosts
        await asyncio.to_thread(manifest_hosts.seed_shipped)
    except Exception:
        logger.exception("The shipped tools' allowlist rows were not refreshed (continuing)")
    try:
        # What a failed rollback of an install left goes first.
        from services.community import community_installer
        asides = await asyncio.to_thread(community_installer.sweep_failed_asides)
        if asides:
            logger.info("Tool installs: %s left by a failed install swept", asides)
    except Exception:
        logger.exception("The tool install sweep failed (continuing)")
    # The boot scans the bundled MCPs' environments and builds none: an image
    # carries them, and an install from source builds them with its own
    # setup script. An MCP the scan finds stale is left out of sessions
    # (``tool_envs``), and admins are told once per start which ones and the script.
    try:
        plan = await asyncio.to_thread(mcp_venv_bootstrap.scan_bundled_venvs)
        await asyncio.to_thread(mcp_venv_bootstrap.prune_unused_pythons)
    except Exception:
        logger.exception("Bundled MCP venv scan failed (non-fatal)")
        plan = None
    if plan is not None:
        notice = mcp_venv_bootstrap.build_notice(plan) if plan.steps else ""
        tool_envs.note_stale(plan.names, notice)
        if notice:
            # The admins' notification goes out once the start is over (``_boot_continue``).
            logger.warning("%s", notice)

    _bg_tasks.append(asyncio.create_task(_docker_mcp_bringup(docker_manager)))
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


def _boot_subscriptions() -> None:
    """The subscriptions: the hosted instances, the session counters, the
    stale bindings, the usage-limit rests, the model sync, the retired remaps."""
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
            logger.info("Pruned %s stale session-subscription binding(s)", pruned)
        # Rows whose subscription is gone (deleted or replaced while a
        # session was bound) — no TTL saves them: they would bind a
        # re-adopted session to nothing and attribute usage to a ghost.
        orphans = subscription_store.prune_orphan_session_bindings()
        if orphans:
            logger.info("Pruned %s orphan session-subscription binding(s)", orphans)
    except Exception:
        logger.exception("session-binding prune failed (non-fatal)")
    # The usage-limit rests (subscription_rests): an account that reached its
    # limit stays out of the selection across the restart.
    try:
        from services.engines import subscription_pool
        rests = subscription_pool.load_rests()
        if rests:
            logger.info("Loaded %s usage-limit rest(s)", rests)
    except Exception:
        logger.exception("loading the usage-limit rests failed (non-fatal)")
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
    logger.info("Synced builtin models for %s execution layer(s)", len(_caps))


async def _boot_services() -> None:
    """The services and the background loops: concurrency, the scheduler,
    notifications, the reapers, the gateway push, the sweeps, the tunnel,
    the broadcasters, the six workers."""
    # Initialize concurrency control (reads limits from DB)
    from core import concurrency
    concurrency.init()
    scheduler.start()
    # Initialize notification system (after scheduler so it can register jobs)
    notification_manager.start()
    _bg_tasks.append(asyncio.create_task(reap_idle_direct_sessions()))
    logger.info(
        "Direct session reaper started "
        "(timeout=%ss)", config.DIRECT_SESSION_TIMEOUT
    )
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
    # The SSH signer's sockets die with the session; those of an earlier
    # process are swept. The credential exit runs on the machines, so the
    # authority an earlier release kept here goes, and so do the private key
    # files earlier releases wrote into the scopes' key dirs.
    from core.credentials import (
        credential_exit as _credential_exit,
        ssh_signer as _ssh_signer,
    )
    _ssh_signer.register_purge_hook()
    _ssh_signer.sweep_at_boot()
    _credential_exit.register_purge_hook()
    await asyncio.to_thread(_credential_exit.retire_platform_authority)
    from core.sandbox import session_config_dir as _scd
    await asyncio.to_thread(_scd.sweep_private_ssh_keys)
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

    _bg_tasks.append(asyncio.create_task(asyncio.to_thread(_prewarm_language_identifier)))
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
    from services.scheduler import run_recovery
    _sat_cm = get_connection_manager()
    # Wire Mode C run-recovery: the satellite's post-auth sessions_alive report
    # re-adopts parked in-flight runs. Registered before the heartbeat monitor
    # starts so the first reconnect's report is handled.
    run_recovery.register(_sat_cm)
    from services.remote import compute_bootstrap
    compute_bootstrap.register()
    from services.apps import app_compute
    app_compute.register()
    # After it by registration order: the terminals taken back at a report,
    # and the sessions a machine came back without.
    from services.remote import machine_sessions
    machine_sessions.register()
    # The update that waits for a busy machine to go idle.
    from ws import satellite as satellite_ws
    satellite_ws.register()
    _bg_tasks.append(asyncio.create_task(_sat_cm.heartbeat_monitor()))
    logger.info("Satellite heartbeat monitor started")

    # The planned stop's counters and hooks (``core.drain``).
    import startup_drain
    startup_drain.register()
    _bg_tasks.append(asyncio.create_task(_boot_continue()))
    _bg_tasks.append(asyncio.create_task(_alive_beat()))
    from services.infra import health_parts
    _bg_tasks.append(asyncio.create_task(health_parts.sample_loop()))
    _bg_tasks.append(asyncio.create_task(startup_drain.activity_loop()))
    _bg_tasks.append(asyncio.create_task(_boot_relay_events()))

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
    from core.remote import install_registry as _install_registry
    from core.session import warmup_registry as _warmup_registry

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

















    sweep_items = _sweep_items(_warmup_registry, _install_registry, _transfer_registry)
    _bg_tasks.append(asyncio.create_task(_registry_sweep_loop(sweep_items, _sweep_ticks)))
    logger.info("Warmup + install registry sweeper started (60s interval)")
    from api.media import uploads as _uploads
    _bg_tasks.append(asyncio.create_task(_uploads.staging_sweep_loop()))


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

    # The daily release check: admins are told when a newer release exists.
    # Not started when air-gapped, on the cloud or switched off by its key.
    from services.infra import release_check
    release_check.start_worker()

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


def _boot_arm(app: FastAPI, default_executor: ThreadPoolExecutor) -> None:
    """The last step before the first request: the routes warmed, the loop
    pool, the watchdog."""
    # The first request that falls through every route would otherwise pay
    # FastAPI's lazy route-state build on the loop; pay it now.
    try:
        warm_routes(app)
    except Exception:
        logger.exception("route warm-up failed (non-fatal)")

    _note_stop_signal()

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


@asynccontextmanager
async def lifespan(app: FastAPI):
    from core.events.stream_pump import note_platform_ending, note_platform_running
    note_platform_running()
    default_executor = _boot_process()
    await _boot_database()
    await _boot_registrations()
    await _boot_mcps()
    _boot_subscriptions()
    await _boot_services()
    _boot_arm(app, default_executor)

    yield
    # ── Graceful Shutdown ──
    logger.info("Proxy shutdown starting...")
    note_platform_ending()
    await _stop_workers()

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
