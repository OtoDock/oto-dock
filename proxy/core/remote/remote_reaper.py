"""The idle reaper for remote sessions.

``reap_idle_remote_sessions`` is the background task ``startup.py`` runs: it
closes remote sessions that went idle past the platform timeout or lost
their satellite, with the same in-flight and pending-work leashes the local
reaper applies, and walks the chat moves waiting for their target's proof
(``services/remote/chat_move.sweep``). Its tick decides with no await and
waits on nothing: each machine's sessions are probed and closed in a task
of that machine's own, each sweep runs as a task of its own, and a machine
or a sweep still busy at the next tick is skipped by it. Split out of
remote_execution.py, which re-exports both names.
"""

import asyncio
import logging
import time

logger = logging.getLogger("remote-layer")


def _idle_session_has_pending_work(sid: str, now: float, idle_timeout: float) -> bool:
    """Between-turns leash for the idle reaper: a warm session whose subagent children are
    still running — e.g. a gracefully-aborted lane whose interrupt resolved
    the registry but not the child processes — must not be closed under them
    while hooks still fire. A silent subagent fires no hooks until its Stop —
    the registry-pending check is the leg that covers that window."""
    from core.session.session_state import (
        get_hook_activity,
        get_subagent_registry,
    )
    last_hook = get_hook_activity(sid)
    # Truthiness, not `is not None`: get_hook_activity returns 0 for a
    # session that never fired a hook, and `now - 0 <= idle_timeout` is
    # true for the first idle_timeout seconds after HOST boot (monotonic
    # starts near 0) — which suppressed reaping on freshly booted servers.
    if last_hook and now - last_hook <= idle_timeout:
        return True
    if get_subagent_registry(sid).has_pending:
        return True
    # A running background COMMAND completes on the satellite's forwarded
    # stdout, which the satellite forwards as long as the proxy's bound: the
    # session is kept so the next turn's stale drain reads the command's end.
    from core.session.background_leash import pending_background
    return pending_background(sid)[0] > 0


# One at a time each: a sweep, or one machine's reaping, that still runs when
# the next tick comes is skipped by that tick. So a machine that does not
# answer holds its own closes back, never another machine's, and the tick
# itself waits on nothing.
_running: dict[str, asyncio.Task] = {}

KEEP, REAP, PROBE = "keep", "reap", "probe"


def _start_once(key: str, make) -> bool:
    task = _running.get(key)
    if task is not None and not task.done():
        return False
    from core.background import spawn
    _running[key] = spawn(make(), name=key, owner="remote_reaper")
    return True


async def settle() -> None:
    """Wait for every sweep and reap a tick started (tests, shutdown)."""
    tasks = [t for t in _running.values() if not t.done()]
    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)


async def _sweep(what: str, run) -> None:
    try:
        await run()
    except Exception:
        logger.exception("%s failed", what)


def _verdict(layer, sid: str, info, now: float, idle_timeout: float) -> str:
    """What the reaper does with one session now, with no await: ``KEEP``,
    ``REAP``, or ``PROBE`` (an in-flight turn gone silent: reaped only when
    its machine says the process is dead)."""
    import config as app_config
    from core.session import session_state as _state
    idle = now - info.last_activity
    # Grace fix: a session held in reconnect-grace (a WS blip where
    # the satellite may return) reports not-connected but must NOT be
    # reaped — the grace machinery re-adopts it on reconnect. Per-
    # session grace for headless (vs. the interactive reaper's
    # per-machine is_pty_in_grace).
    connected = (layer._cm.is_connected(info.machine_id)
                 or layer._cm.is_session_in_grace(info.machine_id, sid))
    if connected and _state.has_pending_prompt(sid):
        return KEEP  # a prompt waits on a person; its own wait bounds it
    if info.cli_dead and not info.turn_active and not layer.is_spawning(sid):
        # Its engine is gone (it exited, a stop ended it, its machine said
        # so): the record holds a slot and a seat for nothing, and the next
        # message starts a session either way. A start of the id under way
        # replaces the record itself.
        return REAP
    if info.turn_active and connected and idle <= app_config.CLAUDE_TIMEOUT:
        # Mid-turn event silence is not idleness: a network stall
        # on the satellite box leaves the CLI alive and working
        # with zero stream events (Mode D). Give in-flight turns
        # the CLI turn ceiling, and inside it reap only on a
        # probe-confirmed dead process.
        return PROBE if idle > idle_timeout else KEEP
    if idle > idle_timeout or not connected:
        # Between-turns leash, bounded by the CLI turn ceiling so
        # a stuck subagent can't pin the session forever.
        if (connected and idle <= app_config.BACKGROUND_WORK_CEILING_S
                and _idle_session_has_pending_work(sid, now, idle_timeout)):
            return KEEP
        return REAP
    return KEEP


async def _reap_machine(layer, machine_id: str, sids: list[str], idle_timeout: float) -> None:
    """One machine's sessions, one after another: each judged again right
    before its close (the closes before it may have taken a while), probed
    when its turn is in flight, then closed."""
    for sid in sids:
        try:
            info = layer._sessions.get(sid)
            if info is None or info.machine_id != machine_id:
                continue
            verdict = _verdict(layer, sid, info, time.monotonic(), idle_timeout)
            if verdict == KEEP:
                continue
            if verdict == PROBE:
                if not await layer.probe_session_process_dead(sid):
                    continue
                logger.warning(
                    "Idle reaper: in-flight turn %s idle %.0fs with a dead process — reaping",
                    sid[:8], time.monotonic() - info.last_activity)
            if info.turn_active and not layer._cm.is_connected(info.machine_id):
                # Its machine is still away: the close tells the turn's
                # reader nothing, so the turn is ended for it first.
                from services.remote import machine_sessions
                await machine_sessions.end_away_turn(layer, info)
            logger.info("Reaping idle remote session %s", sid[:8])
            await layer.close_session(sid)
        except Exception as e:  # noqa: BLE001  # one session's reap must not stop the machine's others
            logger.error("Remote session reaper error: %s", e)


def reap_dead(layer, machine_id: str, sid: str) -> bool:
    """Close one session whose machine just said its engine is gone, without
    waiting for the tick: its slot and its seat are free at once. Judged like
    any other, in a task of its own, so a machine whose reaping runs holds it
    back no more than the session's own close would. False when a reap of
    this session already runs."""
    async def _reap() -> None:
        from core.session import session_state as _state
        await _reap_machine(layer, machine_id, [sid], await _state.cached_idle_timeout())
    return _start_once(f"reap:{machine_id}:{sid}", _reap)


async def reap_once() -> None:
    """One tick: start the three sweeps and one reaping task per machine that
    has something to reap. Waits for none of them."""
    from core.session.session_manager import _get_remote_layer
    from services.remote import chat_move, switch_leftovers
    from services.scheduler import run_recovery

    # Fail parked runs whose satellite never reconnected (Mode C deadline).
    _start_once("sweep:recovery", lambda: _sweep("recovery sweep", run_recovery.sweep_expired))
    # The terminals of a machine away for a day are ended.
    from core.remote import remote_pty
    remote_pty.sweep_away()
    # The moves waiting for the target's proof: the source copy goes.
    _start_once("sweep:chat-move", lambda: _sweep("chat move sweep", chat_move.sweep))
    # The session files an engine switch left, once the new engine answered.
    _start_once("sweep:switch-leftovers",
                lambda: _sweep("engine switch leftovers sweep", switch_leftovers.sweep))
    try:
        layer = _get_remote_layer()
        from core.session import session_state as _state
        idle_timeout = await _state.cached_idle_timeout()
        now = time.monotonic()
        by_machine: dict[str, list[str]] = {}
        for sid, info in list(layer._sessions.items()):
            if _verdict(layer, sid, info, now, idle_timeout) != KEEP:
                by_machine.setdefault(info.machine_id, []).append(sid)
        for machine_id, sids in by_machine.items():
            _start_once(
                f"reap:{machine_id}",
                lambda machine_id=machine_id, sids=sids: _reap_machine(layer, machine_id, sids, idle_timeout))  # type: ignore[misc]  # mypy infers no type for a lambda with default arguments
        for key in [k for k, task in _running.items() if task.done()]:
            _running.pop(key, None)
    except Exception as e:  # noqa: BLE001  # the reaper loop's tick: one failed pass must not stop reaping
        logger.error("Remote session reaper error: %s", e)


async def reap_idle_remote_sessions() -> None:
    """Background task: reap idle remote sessions periodically."""
    # A chat move a restart cut short failed, and its staging goes.
    try:
        from services.remote import chat_move
        await asyncio.to_thread(chat_move.settle_at_start)
    except Exception:
        logger.exception("chat move settle at start failed")
    while True:
        await asyncio.sleep(60)
        await reap_once()
