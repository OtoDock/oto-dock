"""Reconnect-grace lifecycle for satellite connections (mixin).

When a satellite drops, its live sessions are held in a grace window so a quick
reconnect (restart / auto-update / network blip) resumes them instead of tearing
them down. Mixed into SatelliteConnectionManager; split out of
satellite_connection.py. `_GRACE_WINDOW_S` stays in satellite_connection (shared
with deregister + monkeypatched by tests) and is imported lazily where needed.
"""

import asyncio
import logging

logger = logging.getLogger("otodock.satellite")

_GRACE_EXPIRED_MSG = (
    "⚠ stream interrupted — the remote machine disconnected and did not "
    "reconnect in time; output may be incomplete"
)



class SatelliteGraceMixin:
    async def _expire_grace(self, machine_id: str) -> None:
        """Reconnect-grace timeout. If the satellite hasn't reconnected within
        _GRACE_WINDOW_S, terminate the held in-flight sessions with a visible
        ⚠-incomplete marker instead of a silent truncation.
        A reconnect (register) or a close/abort (drop_grace_session) cancels
        this timer first + pops `_grace_sessions`, so reaching here with a
        non-empty `held` means the window genuinely lapsed."""
        # _GRACE_WINDOW_S stays in satellite_connection (shared with deregister +
        # monkeypatched by tests) — read it live each call.
        from core.remote.satellite_connection import _GRACE_WINDOW_S
        try:
            await asyncio.sleep(_GRACE_WINDOW_S)
        except asyncio.CancelledError:
            return
        async with self._lock:
            held = self._grace_sessions.pop(machine_id, None)
            self._grace_timers.pop(machine_id, None)
        if not held:
            return
        for session_id, (queue, _path) in held.items():
            try:
                # `durable_marker` tells the pump to PERSIST this as a visible
                # block, not just forward it live, so a
                # refresh after a genuinely-lost turn shows the ⚠ instead of a
                # silent truncation. Ordinary transient errors omit the flag.
                queue.put_nowait({
                    "type": "error",
                    "message": _GRACE_EXPIRED_MSG,
                    "durable_marker": True,
                })
                queue.put_nowait(None)  # DONE sentinel — finalize the turn
            except asyncio.QueueFull:
                pass
        logger.info(
            "Satellite %s: reconnect grace expired, terminated %d held "
            "session(s)", machine_id[:8], len(held),
        )

    async def _expire_pty_grace(self, machine_id: str) -> None:
        """Reconnect-grace timeout for interactive PTYs. If the
        satellite hasn't reconnected + reconciled (`pty_alive`) within
        _GRACE_WINDOW_S, tear the held PTYs down — today's immediate behavior,
        just delayed → InteractiveSession teardown + resume-on-send. A
        reconnect's `_reconcile_ptys` cancels this timer first."""
        from core.remote.satellite_connection import _GRACE_WINDOW_S
        try:
            await asyncio.sleep(_GRACE_WINDOW_S)
        except asyncio.CancelledError:
            return
        from core.remote import remote_pty
        async with self._lock:
            self._pty_grace_timers.pop(machine_id, None)
            remote_pty.cancel_machine_ptys(machine_id)
        logger.info(
            "Satellite %s: interactive PTY reconnect grace expired", machine_id[:8],
        )

    async def _reconcile_ptys(self, machine_id: str, alive_session_ids: list) -> None:
        """Reconcile our interactive PTY handles against the satellite's live set
        on reconnect, driven by the satellite's post-auth `pty_alive`.
        Re-adopts the still-alive ones (cancel the grace timer + repaint), exits
        the ones whose child died during the blip, and closes the orphans the
        satellite still runs but we no longer want (kills the reverse-orphan)."""
        from core.remote import remote_pty
        async with self._lock:
            timer = self._pty_grace_timers.pop(machine_id, None)
            if timer is not None and not timer.done():
                timer.cancel()
            readopted, exited, orphans = remote_pty.reconcile_machine_ptys(
                machine_id, alive_session_ids,
            )
        # Side effects OUTSIDE the lock: viewer notifications + orphan reaping
        # (which sends a WS frame). `exited` already fired teardown inside.
        for sid in readopted:
            self._notify_pty_status(sid, "reconnected")
        for sid in orphans:
            await self.send_fire_and_forget(
                machine_id, {"type": "pty_close", "session_id": sid},
            )
        if readopted or exited or orphans:
            logger.info(
                "Satellite %s PTY reconcile: re-adopted %d, exited %d, "
                "closed %d orphan(s)", machine_id[:8],
                len(readopted), len(exited), len(orphans),
            )

    def is_pty_in_grace(self, machine_id: str) -> bool:
        """True when a machine's interactive PTYs are held in reconnect-grace:
        the satellite dropped but may reconnect within the window. The
        idle reaper must skip these (still `alive` but transiently unviewable) and
        a (re)attaching viewer shows "reconnecting". Per-machine — all of a
        machine's PTYs share one grace window."""
        return machine_id in self._pty_grace_timers

    def _notify_pty_status(self, session_id: str, state: str) -> None:
        """Fire the InteractiveSession's on_status (reconnecting/reconnected) for
        the dashboard viewer. No-op if the session is gone or has no viewer."""
        from core.session import interactive_session
        sess = interactive_session.get(session_id)
        if sess is not None:
            sess.notify_status(state)

    def is_session_in_grace(self, machine_id: str, session_id: str) -> bool:
        """True when a session's event queue is held in the reconnect-grace
        area (its satellite dropped but may reconnect within the window). The
        session is 'reconnecting', not severed — the dashboard reap must treat
        it as live (see is_session_stream_attached + the layer's
        session_idle_seconds, both of which special-case grace)."""
        held = self._grace_sessions.get(machine_id)
        return bool(held and session_id in held)

    def drop_grace_session(self, machine_id: str, session_id: str) -> None:
        """Drop a single session from the reconnect-grace area (the user
        aborted, or it was closed during grace) so a later reconnect does NOT
        re-adopt + resume an abandoned turn. Cancels the machine's grace timer
        once its last held session is gone."""
        held = self._grace_sessions.get(machine_id)
        if not held:
            return
        held.pop(session_id, None)
        if not held:
            self._grace_sessions.pop(machine_id, None)
            timer = self._grace_timers.pop(machine_id, None)
            if timer is not None and not timer.done():
                timer.cancel()
