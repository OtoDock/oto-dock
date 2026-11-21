"""What the proxy does on its way down: stops the workers, flushes the pumps,
closes sessions, and arms the exit failsafe last.
"""

import asyncio
import contextlib
import logging
import os
import threading

from core.events.stream_pump import _active_pumps
from services.scheduler import scheduler
from storage import database as task_store, pg as pg_pool

logger = logging.getLogger("otodock")

# Background tasks the lifespan owns — cancelled in ``_shutdown_cleanup`` so
# their teardown never races the closed DB pool.
_bg_tasks: list[asyncio.Task] = []

# Hard-exit failsafe (armed as the LAST shutdown step): non-daemon threads —
# asyncio's default-executor workers above all — are joined by the interpreter
# AFTER uvicorn logs "Finished server process"; one blocked worker used to
# hang every stop until docker's 10s SIGKILL (exit 137). Tests flip the flag.
_ARM_EXIT_FAILSAFE = True
_EXIT_FAILSAFE_GRACE_S = 3.0
# How long the local terminals get to close at the stop.
_TERMINAL_CLOSE_S = 10.0


async def _stop_workers() -> None:
    """The six workers stop in order, a failure logged and the next still
    stopped."""
    from services.billing import license_check_worker
    from services.engines import token_fanout
    from services.infra import release_check
    from services.oauth import oauth_refresh_worker
    from services.phone import phone_health_worker
    from services.webhooks import subscription_renewer
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
        await release_check.stop_worker()
    except Exception:
        logger.exception("Release check worker shutdown failed (continuing)")

    try:
        await phone_health_worker.stop_worker()
    except Exception:
        logger.exception("Phone health worker shutdown failed (continuing)")

    try:
        await token_fanout.stop_worker()
    except Exception:
        logger.exception("Token-freshness worker shutdown failed (continuing)")


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
            # A turn the engine started by itself replays nothing: what it
            # wrote is kept now, the rest is read after the restart
            # (``take_held_lines``).
            if not pump.records_engine_turn and run_recovery.is_recovery_eligible(chat_id):
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
        except Exception as e:  # noqa: BLE001  # the shutdown's per-pump step: one pump must not stop the rest
            logger.warning("Shutdown: pump flush %s error: %s", chat_id[:8], e)
    if waits:
        with contextlib.suppress(Exception):
            await asyncio.wait(waits, timeout=5)
        logger.info("Shutdown: flushed %s open pump(s)", len(waits))


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
    # the runs the next boot takes up again: a remote run whose CLI is still
    # alive on its satellite (parked and re-adopted there), and every run
    # that reopens (``run_recovery.give_up_at_shutdown``).
    try:
        from services.scheduler import run_recovery
        keep = run_recovery.give_up_at_shutdown()
        orphaned_runs = task_store.mark_orphaned_runs_failed(exclude_ids=keep)
        orphaned_meetings = task_store.mark_orphaned_meetings_failed()
        if orphaned_runs or orphaned_meetings or keep:
            logger.info(
                "Shutdown cleanup: marked %s run(s) and "
                "%s meeting(s) as failed; "
                "left %s recoverable run(s) running",
                orphaned_runs, orphaned_meetings, len(keep)
            )
    except Exception as e:  # noqa: BLE001  # a shutdown step: a failure must not skip the steps after it
        logger.error("Shutdown DB cleanup failed: %s", e)

    # A Docker MCP build or pull still running (the boot's bring-up) ends
    # with its process group: the worker threads it holds would keep the
    # process alive until the build's own timeout.
    try:
        from services.mcp import docker_manager
        ended = await asyncio.to_thread(docker_manager.end_running_compose)
        if ended:
            logger.info("Shutdown: ended %s Docker compose run(s)", ended)
    except Exception as e:  # noqa: BLE001  # a shutdown step: a failure must not skip the steps after it
        logger.error("Ending the Docker compose runs failed: %s", e)

    # Cancel the lifespan's own background loops so their teardown doesn't
    # race the closed pool below (asyncio.run would cancel them anyway —
    # but only AFTER lifespan, with the pool already gone).
    # Beside them, the fire-and-forget tasks started through
    # core.background.spawn get a grace period to finish (most belong to the
    # sessions closed above), then a cancel; both waits share the 3 s and the
    # teardown still has the pool.
    from core import background
    for task in _bg_tasks:
        task.cancel()

    async def _wait_bg_tasks() -> None:
        if _bg_tasks:
            with contextlib.suppress(Exception):
                await asyncio.wait(_bg_tasks, timeout=3)

    waited = await asyncio.gather(
        _wait_bg_tasks(), background.cancel_all(grace=2.0, timeout=1.0),
        return_exceptions=True,
    )
    _bg_tasks.clear()
    if isinstance(waited[1], BaseException):
        logger.error("Shutdown: background task cancel failed (continuing): %r", waited[1])
    elif waited[1]:
        logger.info("Shutdown: cancelled %s background task(s) still running after 2 s", waited[1])

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
    _note_the_clean_stop(logger)
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
    # Side by side: one after another, a few sessions that wait out their
    # own close would use up the whole stop.
    from core.session.session_manager import holding_engines

    async def _close(path: str, layer, sid: str) -> None:
        try:
            await layer.close_session(sid)
        except Exception as e:  # noqa: BLE001  # a shutdown close: one session must not stop the rest
            logger.warning("Shutdown: %s %s error: %s", path, sid[:8], e)

    closes = [_close(path, layer, sid) for path, layer in holding_engines().items()
              for sid in layer.local_session_ids()]
    # The terminals this host runs: each tails its transcript into its
    # chat's rows, keeps its open turn's record and answers no card.
    from core import drain
    from core.session import interactive_session
    closes.append(interactive_session.close_all(
        drain.PLATFORM_STOPPING, local_only=True, timeout=_TERMINAL_CLOSE_S))
    await asyncio.gather(*closes)

    from core.session.session_manager import _remote_layer
    if _remote_layer:
        await _shutdown_remote_sessions(_remote_layer, logger)


async def _shutdown_remote_sessions(remote_layer, logger) -> None:
    """Remote sessions at shutdown. One whose engine takes a session back
    after a restart (``readopts_idle_session``: Claude and Codex) is LEFT
    OPEN, idle or mid-turn, with its security context: the satellite keeps
    its process and reports it, and the new process re-adopts it (a turn in
    flight is replayed, Claude's and Codex's alike, and an idle session is
    taken back). Closing it here could not reach the satellite anyway
    (uvicorn closes the sockets before the lifespan's shutdown) and would
    only drop the context, so the session came back with every hook refused.
    Other engines close as before."""
    from core.session.session_manager import get_layer_capabilities
    for sid in remote_layer.local_session_ids():
        info = remote_layer._sessions.get(sid)
        caps = get_layer_capabilities(getattr(info, "execution_path", "") or "") if info else None
        if caps is not None and caps.runtime.readopts_idle_session:
            logger.info(
                "Shutdown: leaving remote %s open for re-adoption "
                "(%s)",
                sid[:8], 'mid-turn' if getattr(info, 'turn_active', False) else 'idle'
            )
            continue
        try:
            await remote_layer.close_session(sid)
        except Exception as e:  # noqa: BLE001  # a shutdown close: one session must not stop the rest
            logger.warning("Shutdown: Remote %s error: %s", sid[:8], e)


def _note_the_clean_stop(logger) -> None:
    """The stop was the platform's own, said while the database is still
    there: the next boot reads the turns it finds as cut by a stop. Only
    once the compute machine drained too: a turn it still runs outlives this
    stop, and a controller that stops the host after the mark would cut it
    (the next boot then reads it as a crash)."""
    try:
        from core import drain
        from services.infra import platform_marks
        from startup_drain import compute_open
        still = compute_open()
        if still:
            logger.warning("Shutdown: the clean stop is not recorded: the compute machine still "
                           "runs %d open turn(s)", still)
            return
        platform_marks.note_clean_stop()
        logger.info("Shutdown: the clean stop is recorded, open at the stop: %s", drain.open_work())
    except Exception:
        logger.exception("Shutdown: the clean stop was not recorded (continuing)")
