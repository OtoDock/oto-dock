"""The idle reaper for remote sessions.

``reap_idle_remote_sessions`` is the background task ``startup.py`` runs: it
closes remote sessions that went idle past the platform timeout or lost
their satellite, with the same in-flight and pending-work leashes the local
reaper applies. Split out of remote_execution.py, which re-exports both
names.
"""

import asyncio
import logging
import time

logger = logging.getLogger("remote-layer")


def _idle_session_has_pending_work(
    sid: str, now: float, idle_timeout: float,
) -> bool:
    """Between-turns leash for the idle reaper (mirrors the LOCAL reaper's
    hook-activity extension): a warm session whose subagent children are
    still running — e.g. a gracefully-aborted lane whose interrupt resolved
    the registry but not the child processes — must not be closed under them
    while hooks still fire. A silent subagent fires no hooks until its Stop —
    the registry-pending check is the leg that covers that window."""
    from core.session.session_state import (
        get_hook_activity, get_subagent_registry,
    )
    last_hook = get_hook_activity(sid)
    # Truthiness, not `is not None`: get_hook_activity returns 0 for a
    # session that never fired a hook, and `now - 0 <= idle_timeout` is
    # true for the first idle_timeout seconds after HOST boot (monotonic
    # starts near 0) — which suppressed reaping on freshly booted servers.
    # The local reaper (cli/session.py) guards the same way.
    if last_hook and now - last_hook <= idle_timeout:
        return True
    return get_subagent_registry(sid).has_pending


async def reap_idle_remote_sessions() -> None:
    """Background task: reap idle remote sessions periodically."""
    import config as app_config
    from core.session.session_manager import _get_remote_layer
    while True:
        await asyncio.sleep(60)
        # Fail parked runs whose satellite never reconnected (Mode C deadline).
        try:
            from services.scheduler import run_recovery
            await run_recovery.sweep_expired()
        except Exception:
            logger.exception("recovery sweep failed")
        try:
            layer = _get_remote_layer()
            now = time.monotonic()
            to_reap: list[str] = []

            idle_timeout = app_config.get_idle_timeout()
            for sid, info in list(layer._sessions.items()):
                idle = now - info.last_activity
                # Grace fix: a session held in reconnect-grace (a WS blip where
                # the satellite may return) reports not-connected but must NOT be
                # reaped — the grace machinery re-adopts it on reconnect. Per-
                # session grace for headless (vs. the interactive reaper's
                # per-machine is_pty_in_grace).
                connected = (layer._cm.is_connected(info.machine_id)
                             or layer._cm.is_session_in_grace(info.machine_id, sid))
                if info.turn_active and connected:
                    # Mid-turn event silence is not idleness: a network stall
                    # on the satellite box leaves the CLI alive and working
                    # with zero stream events (Mode D). Give in-flight turns
                    # the CLI turn ceiling, and inside it reap only on a
                    # probe-confirmed dead process.
                    if idle <= app_config.CLAUDE_TIMEOUT:
                        if (idle > idle_timeout
                                and await layer.probe_session_process_dead(sid)):
                            logger.warning(
                                f"Idle reaper: in-flight turn {sid[:8]} idle "
                                f"{idle:.0f}s with a dead process — reaping"
                            )
                            to_reap.append(sid)
                        continue
                if idle > idle_timeout or not connected:
                    # Between-turns leash, bounded by the CLI turn ceiling so
                    # a stuck subagent can't pin the session forever.
                    if (connected and idle <= app_config.CLAUDE_TIMEOUT
                            and _idle_session_has_pending_work(
                                sid, now, idle_timeout)):
                        continue
                    to_reap.append(sid)

            for sid in to_reap:
                logger.info(f"Reaping idle remote session {sid[:8]}")
                await layer.close_session(sid)
        except Exception as e:
            logger.error(f"Remote session reaper error: {e}")
