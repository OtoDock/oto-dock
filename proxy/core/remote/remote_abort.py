"""Aborting a remote turn: soft interrupt, watchdog, hard kill (mixin).

``abort`` (graceful-first with a watchdog that escalates to the kill path),
``_hard_abort``, and the stop-and-send ``interrupt_for_queued``. The
watchdog window and the strong-reference task set live here, next to the
only code that reads them. Mixed into RemoteExecutionLayer; split out of
remote_execution.py.
"""

import asyncio
import logging
import time

from core.session.session_state import resolve_session_permissions

logger = logging.getLogger("remote-layer")

# Soft-interrupt escalation window. Longer than the local layer's 8s: the
# frame rides the satellite WS and the CLI's result event rides back the
# same way, so budget for two hops plus the CLI's own reaction time.
# 30s (was 12): a CLI mid-TOOL on a slow machine legitimately needs longer
# to close the turn after control_request{interrupt} — live 2026-08-12 an
# old laptop mid-WebSearch missed 12s, the escalation tree-killed the CLI
# and every later spoken turn errored (duplex had no dead-session heal
# then; ws/headless_resume.py now respawns, this window just makes the
# trigger rare). The window is invisible to users: barge-in already cut the
# TTS and Stop already acked — this only delays the wedge repair for
# genuinely dead/stuck processes.
_REMOTE_INTERRUPT_WATCHDOG_S = 30.0

# Strong refs — a bare create_task is GC-collectable mid-flight.
_remote_watchdog_tasks: set[asyncio.Task] = set()


class RemoteAbortMixin:
    # --- ExecutionLayer interface: abort ---

    async def abort(self, session_id: str) -> bool:
        """Abort the in-flight turn — graceful-first for every engine.

        Graceful path (live turn, satellite ≥ 0.5.89): send the engine's
        soft-interrupt frame (``RemoteEngineAdapter.soft_interrupt_frame`` —
        Claude's ``interrupt_turn`` writes the same ``control_request
        {interrupt}`` the local layer uses into the CLI's stdin and the
        process + MCP sidecars SURVIVE for the next prompt; Codex's is the
        regular ``abort``, which its satellite twin handles softly with
        ``turn/interrupt`` while its persistent forwarder keeps relaying the
        closing turn's tail — the deployed satellites' ``interrupt_turn``
        handler is CLI-only). The ``session_aborted`` ack the satellite
        sends back drains the event queue only when a HARD abort armed it
        (see satellite_connection), so it can't steal the closing turn's
        tail. Either way the producer/pump keep running to persist the
        partial turn, and a watchdog falls back to the hard path if the turn
        doesn't close.

        Hard path (no live turn, older satellite, send failure, or watchdog
        escalation): the satellite kills the process (an engine whose hard
        abort leaves its daemon warm soft-interrupts instead), and the caller
        keeps the producer-cancel + cancelled-context injection.
        """
        info = self._sessions.get(session_id)
        if not info:
            return False
        if (
            info.turn_active
            and self._cm.satellite_supports_soft_interrupt(info.machine_id)
        ):
            # Same release as the hard path: a pending hook/native permission
            # waiter must not strand the turn we just asked to close.
            resolve_session_permissions(session_id, approved=False)
            frame = self._adapter(info).soft_interrupt_frame()
            try:
                await self._cm.send_fire_and_forget(info.machine_id, {
                    "type": frame,
                    "session_id": session_id,
                })
            except Exception as e:
                logger.warning(
                    "Remote soft interrupt send failed for session %s: %s — "
                    "falling back to hard abort", session_id[:8], e,
                )
            else:
                _wd = asyncio.create_task(
                    self._soft_interrupt_watchdog(
                        session_id, info.current_send_command_id,
                    ),
                    name=f"remote-interrupt-watchdog-{session_id[:8]}",
                )
                _remote_watchdog_tasks.add(_wd)
                _wd.add_done_callback(_remote_watchdog_tasks.discard)
                return True
        return await self._hard_abort(session_id)

    async def _hard_abort(self, session_id: str) -> bool:
        """Kill-path abort: the satellite tree-kills the CLI subprocess (and
        its codex twin marks the turn aborted before the flush), so the caller
        keeps today's producer-cancel + cancelled-context injection."""
        info = self._sessions.get(session_id)
        if not info:
            return False
        info.proxy_killed = True
        # If the session was held in reconnect-grace, drop it — the
        # user aborted, so a reconnect must NOT re-adopt + resume this turn.
        self._cm.drop_grace_session(info.machine_id, session_id)
        # Release any proxy-side permission waiter for this remote session
        # (the satellite Codex bridge / stdio gate block here via the tunnel) so
        # an abort denies it promptly instead of hanging the daemon's child.
        resolve_session_permissions(session_id, approved=False)
        # Arm the abort-ack event BEFORE sending so the satellite's
        # `session_aborted` reply (sent after the graceful kill + output flush)
        # can't be missed by a race. The auto-resume path awaits this so the
        # next turn starts only once the dying CLI is gone and its flushed
        # events have been drained.
        self._cm.arm_abort_acked(info.machine_id, session_id)
        # Fire-and-forget the abort command itself: we don't block the Stop
        # button on it (the dashboard gets `aborted` immediately); the
        # confirmation rides back asynchronously as `session_aborted`.
        try:
            await self._cm.send_fire_and_forget(info.machine_id, {
                "type": "abort",
                "session_id": session_id,
            })
        except Exception as e:
            logger.warning("Remote abort send failed for session %s: %s",
                           session_id[:8], e)
        # An engine whose hard abort kills its process (Claude: the persistent
        # subprocess exits as part of the abort) is flagged dead so the
        # dashboard's auto-resume path (``is_session_process_dead``) spawns a
        # fresh session on the next user message — without this flag, the
        # satellite would answer the next ``send_message`` with ``RuntimeError:
        # CLI process not running``. An engine whose daemon soft-interrupts
        # its turn and stays warm (Codex) is not.
        if self.capabilities_for(session_id).runtime.hard_abort_kills_process:
            info.cli_dead = True
        return False

    async def interrupt_for_queued(self, session_id: str) -> bool:
        """Stop-and-send for a REMOTE session of an engine that supports it
        (``behaviour.supports_interrupt_for_queued`` — Claude) — the graceful
        arm of ``abort`` only: relay the engine's soft-interrupt frame (the
        satellite writes the same ``control_request {interrupt}`` into the
        CLI's stdin; process + MCP sidecars + background tasks survive). NO
        ``_soft_interrupt_watchdog``, NO ``_hard_abort`` — an ignored
        interrupt degrades to the queued message draining at the natural turn
        end. Send-THEN-resolve (opposite of ``abort``): ``send_fire_and_forget``
        silently no-ops on a disconnected machine, and a failed send must
        never deny a parked permission blind — that would un-park the turn
        with no interrupt delivered.
        """
        info = self._sessions.get(session_id)
        if (
            info is None
            or not info.turn_active
            or not self.capabilities_for(session_id).behaviour.supports_interrupt_for_queued
            or not self._cm.is_connected(info.machine_id)
            or not self._cm.satellite_supports_soft_interrupt(info.machine_id)
        ):
            return False
        try:
            await self._cm.send_fire_and_forget(info.machine_id, {
                "type": self._adapter(info).soft_interrupt_frame(),
                "session_id": session_id,
            })
        except Exception as e:
            logger.warning(
                "Remote stop-and-send interrupt failed for session %s: %s",
                session_id[:8], e,
            )
            return False
        resolve_session_permissions(session_id, approved=False)
        return True

    async def _soft_interrupt_watchdog(
        self, session_id: str, armed_cmd_id: str,
    ) -> None:
        """Fall back to the hard abort when a soft interrupt fails to close
        the remote turn (dead CLI process, dropped frame, wedged stream).

        ``armed_cmd_id`` pins the watch to the interrupted turn so a slow
        watchdog can never kill a successor turn. On escalation the CLI
        history may have lost the partial turn — flip the chat's graceful
        flag back so the next turn re-injects the cancelled context (the ws
        abort site stamped graceful=True optimistically). Mirrors the local
        layer's ``_interrupt_watchdog``.
        """
        deadline = time.monotonic() + _REMOTE_INTERRUPT_WATCHDOG_S
        while time.monotonic() < deadline:
            info = self._sessions.get(session_id)
            if info is None or not info.alive:
                return
            if (not info.turn_active
                    or info.current_send_command_id != armed_cmd_id):
                return  # the interrupted turn closed gracefully
            await asyncio.sleep(0.25)
        info = self._sessions.get(session_id)
        if (info is None or not info.alive or not info.turn_active
                or info.current_send_command_id != armed_cmd_id):
            return
        logger.warning(
            "Remote session %s: soft interrupt did not close the turn within "
            "%.0fs — falling back to hard abort", session_id[:8],
            _REMOTE_INTERRUPT_WATCHDOG_S,
        )
        await self._hard_abort(session_id)
        try:
            from storage import database as task_store
            chat = task_store.get_chat_by_session(session_id)
            if chat and chat.get("last_turn_aborted"):
                task_store.update_chat(chat["id"], last_abort_graceful=False)
        except Exception:
            logger.exception(
                "Remote session %s: watchdog graceful-flag reset failed",
                session_id[:8],
            )
