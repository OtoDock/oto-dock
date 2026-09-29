"""Server-initiated work delivered via the notify queue: notification forwards,
the server-kicked first turn, and bg-nudge / task-result review turns on their
originating chats.

ServerNotificationController is a mixin of ``DashboardConnection`` (ws/dashboard.py) — methods run
with the connection's full attribute state; nothing here is standalone.
Behavior is pinned by tests/session/test_ws_dashboard_*.
"""

import json
import logging
from typing import TYPE_CHECKING
import config
from storage import database as task_store
from storage.automation import run_status
from core.session import visibility as _vis
from ws import chat_phase
from ws import wire_events as wire

if TYPE_CHECKING:  # the annotation below is a string; nothing runs at import
    from core.execution_layer import ExecutionLayer

logger = logging.getLogger("claude-proxy")

# Frames whose per-user broadcast duplicates a direct send to the acting
# socket (``dashboard_chat_send`` / ``dashboard_dispatch``): dropped here as
# they always were — the golden masters pin one copy — until the broadcast
# names its origin connection.
_DIRECT_COPY_ONLY = frozenset({wire.TITLE_UPDATED, wire.ENGINE_SWITCHED})


class ServerNotificationController:
    """Server-initiated work delivered via the notify queue: notification
    forwards, the server-kicked first turn, and bg-nudge / task-result
    review turns on their originating chats."""

    async def _handle_server_notification(self, notification: dict):
        """Process a server-initiated item from the notify queue.

        A catalogued FRAME (``ws.wire_events.FRAMES``) is forwarded to the
        socket verbatim — notifications, file updates, the satellite update
        and install lifecycles, the transfer lifecycle, the end-of-turn
        alert, the chat status and read receipts, the post-turn goal, the
        interactive rows — after the three
        items that need a hand: ``chat_status`` (a stale streaming frame is
        dropped), ``bg_agent_done`` (narrowed to its key) and the
        ``location_request`` envelope (unwrapped). An internal kind
        (``NOTIFY_*``) runs its server turn or clears its badges; anything
        else is logged and dropped.
        """
        ntype = notification.get("type", "")

        if ntype == wire.CHAT_STATUS:
            # Per-chat live-dot signal (pump turn start/end, broadcast to every
            # connection of the chat owner) — forward so the sidebar dot
            # lights/clears for chats generating in the background. The notify
            # queue is only drained BETWEEN the viewed chat's turns, so a
            # turn-START "streaming" can sit queued for a whole turn and land
            # right after `done` — where the frontend's auto-attach reads it as
            # a fresh turn and pointlessly re-resumes the chat (a full-history
            # reload flash at every turn end). Drop a "streaming" whose chat no
            # longer has a live turn; its matching "ready" is right behind.
            if notification.get("status") == chat_phase.STREAMING:
                from core.events.stream_pump import _active_pumps
                from core.session import interactive_session
                cid = notification.get("chat_id") or ""
                pump = _active_pumps.get(cid)
                if pump is None or pump.is_done:
                    isess = interactive_session.find_live_for_chat(cid)
                    if isess is None or not isess.turn_open:
                        return
            await self._send(notification)
            return

        if ntype == wire.BG_AGENT_DONE:
            # Individual subagent completed — forward to frontend so it clears
            # that one widget by tool_use_id (order-independent, no FIFO).
            await self._send({"type": wire.BG_AGENT_DONE,
                         "tool_use_id": notification.get("tool_use_id", "")})
            return

        if ntype == wire.NOTIFY_LOCATION_REQUEST:
            # Forward location request to dashboard (fallback when no pump active)
            await self._send(notification.get("data", notification))
            return

        if ntype in _DIRECT_COPY_ONLY:
            # The acting socket already received this frame directly; the
            # broadcast copy meant for the user's OTHER tabs stays undelivered
            # until the broadcast carries its origin connection (a carried
            # defect, core-seams phase 6 audit A1 — Deferred).
            return

        if ntype in wire.FRAMES:
            # Every other catalogued frame rides verbatim: the notification
            # toast or silent inbox bump, a shared file's change, the
            # satellite update and MCP-install lifecycles (into the machine
            # and install stores), the transfer lifecycle, the end-of-turn
            # ping, the read receipt, the post-turn goal, the interactive
            # history rows.
            await self._send(notification)
            return

        if ntype == wire.NOTIFY_SERVER_KICK:
            # The backgrounded warmup spawn (_spawn_tail) finished
            # and enqueued the server-owned FIRST turn. It runs here, in the main
            # loop (single socket reader). If the user is still on the warmed chat
            # we drive the full turn inline via _handle_chat (attachments,
            # cancelled-context, queued-message drain, streams to the socket); if
            # they navigated away during the spawn we run it HEADLESS on the warmed
            # chat so it lands there with no contamination of the viewed chat.
            kick_cid = notification.get("chat_id", "")
            kick_sid = notification.get("session_id")
            kick_text = notification.get("text", "")
            kick_images = notification.get("images", [])
            kick_files = notification.get("files", [])
            if not kick_text or not kick_cid:
                return
            # Abort-during-spawn race: the spawn finished and enqueued this kick
            # before the user's abort arrived (so _spawn_tail didn't catch it).
            # Kill the session and skip the turn — the client already got `aborted`.
            if self._warmup_abort_chat == kick_cid:
                self._warmup_abort_chat = None
                _k_layer = self.layer if kick_cid == self.chat_id else await self._resolve_layer_for_chat_async(kick_cid)
                if _k_layer and kick_sid:
                    try:
                        await _k_layer.abort(kick_sid)
                    except Exception:
                        logger.warning(f"server-kick abort teardown failed for chat={kick_cid}")
                return
            logger.info(
                f"WS dashboard: server kick drained for chat={kick_cid[:8]} "
                f"(viewed={kick_cid == self.chat_id})"
            )
            if kick_cid == self.chat_id:
                await self._handle_chat({
                    "text": kick_text,
                    "images": kick_images,
                    "files": kick_files,
                    "chat_id": kick_cid,
                    "_server_kick": True,
                    "_focus_line": notification.get("focus_line", ""),
                })
            else:
                await self._run_kick_headless(kick_cid, kick_sid, kick_text, kick_images, kick_files)
            return

        # Server turns (bg_nudge / task_result_prompt) run on their ORIGINATING
        # chat — carried in the notification — NOT the chat this socket is
        # currently viewing (the old contamination bug: they read the connection
        # attributes, so a nudge for chat A landed in chat B after a switch).
        # `_run_server_turn` routes to the right chat's pump (headless if the
        # socket isn't viewing it; the pump persists to the DB regardless). They
        # still fire only BETWEEN the viewed chat's turns — `notify_queue` is
        # drained solely by the main WS loop's `asyncio.wait`, which is reached
        # only between turns (during a turn, control is inside `_enter_pump_loop`).
        # So `streaming` is always False here; no explicit concurrency gate needed.
        if ntype == wire.NOTIFY_BG_NUDGE:
            count = notification["count"]
            # Originating chat/session from the notification (set by the bg
            # monitor, stream_pump.py); fall back to the viewed chat only if
            # absent (shouldn't happen — the monitor always carries them).
            nudge_chat_id = notification.get("chat_id") or self.chat_id
            nudge_sid = notification.get("session_id") or self.session_id
            # Composed by the SAME function as the monitor's direct paths —
            # the payload carries the finished-job labels so the model can
            # tell which agent completed.
            from core.events.pump_bg_monitors import compose_agent_nudge
            labels = notification.get("labels") or []
            nudge = compose_agent_nudge(count, labels)
            # Save the system event on the ORIGINATING chat.
            if nudge_chat_id:
                task_store.add_chat_message(nudge_chat_id, "event", "",
                    event_type=wire.PERSISTED_BG_NUDGE,
                    event_data=json.dumps({"count": count, "labels": labels}))
            # Clear the bg-agent status bar — only if this socket is viewing the
            # originating chat (else it would clear the WRONG chat's badges; a
            # reattach reconstructs the state from live_state/chat_history).
            if nudge_chat_id == self.chat_id:
                await self._send({"type": wire.BG_AGENTS_COMPLETE, "count": count})
            # Drive the review turn on the originating chat (headless if unviewed).
            nudge_layer = self.layer if nudge_chat_id == self.chat_id else await self._resolve_layer_for_chat_async(nudge_chat_id)
            await self._run_server_turn(
                nudge,
                target_session_id=nudge_sid,
                target_chat_id=nudge_chat_id,
                target_layer=nudge_layer,
            )

        elif ntype == wire.NOTIFY_BG_COMMAND_NUDGE:
            # Background bash command(s) finished post-turn (the bg-command
            # monitor in stream_pump.py). Drives the review turn AND clears the
            # badges — mirror of bg_nudge with command wording.
            count = notification["count"]
            nudge_chat_id = notification.get("chat_id") or self.chat_id
            nudge_sid = notification.get("session_id") or self.session_id
            from core.events.pump_bg_monitors import compose_command_nudge
            labels = notification.get("labels") or []
            nudge = compose_command_nudge(count, labels)
            if nudge_chat_id:
                task_store.add_chat_message(nudge_chat_id, "event", "",
                    event_type=wire.PERSISTED_BG_COMMAND_NUDGE,
                    event_data=json.dumps({"count": count, "labels": labels}))
            # Clear the bg-command badges on the viewed socket. The per-command
            # bg_command_done can't deliver post-turn (the turn's pump is gone —
            # app.py _push_to_pump needs an active pump), so this notify-queue
            # path is the reliable clear, exactly like bg_nudge → bg_agents_complete.
            if nudge_chat_id == self.chat_id:
                await self._send({"type": wire.BG_COMMANDS_COMPLETE, "count": count})
            nudge_layer = self.layer if nudge_chat_id == self.chat_id else await self._resolve_layer_for_chat_async(nudge_chat_id)
            await self._run_server_turn(
                nudge,
                target_session_id=nudge_sid,
                target_chat_id=nudge_chat_id,
                target_layer=nudge_layer,
            )

        elif ntype == wire.NOTIFY_LIVENESS_CLEAR:
            # A session died with liveness still showing (its own lifecycle
            # events can no longer clear the badges — clear_session_liveness
            # broadcast this). Emit the cohort-clear frames if this socket is
            # viewing the affected chat. No server turn, no persistence: the
            # session is dead, and a DB reload already renders history blocks
            # inactive — only the live view needs the clears.
            dead_chat = notification.get("chat_id", "")
            if dead_chat and dead_chat == self.chat_id:
                await self._send({"type": wire.BG_AGENTS_COMPLETE, "count": 0})
                await self._send({"type": wire.BG_COMMANDS_COMPLETE, "count": 0})
                await self._send({"type": wire.FG_AGENTS_COMPLETE})
                logger.info(
                    f"WS dashboard: cleared liveness badges for chat={dead_chat[:8]} "
                    f"(session death: {notification.get('reason') or '-'})"
                )

        elif ntype == wire.NOTIFY_CHAT_UI_FRAME:
            # A chat-scoped UI frame broadcast independent of any delivery
            # path (session_state.broadcast_chat_frame) — e.g. the terminal
            # delegate_result badge frame when the wake landed on a rung with
            # no socket (pty/persistent/one-shot/none). Forward verbatim to
            # the viewer of that chat; no persistence (the ladder already
            # persisted the event row), no server turn.
            if notification.get("chat_id") == self.chat_id:
                frame = notification.get("frame")
                if isinstance(frame, dict) and frame:
                    await self._send(frame)

        elif ntype == wire.NOTIFY_TASK_RESULT_PROMPT:
            task_id = notification.get("task_id", "")
            task_name = notification["task_name"]
            result_prompt = notification["result_prompt"]
            delegate_agent = notification.get("delegate_agent", "")
            output_preview = notification.get("output_text", "")
            # A run_status.DELEGATE_RESULTS word — drives the badge icon;
            # user_interrupted marks a lane the user stopped/steered.
            result_status = notification.get("status", run_status.COMPLETED)
            # Delegating chat/session from the notification (scheduler.py now
            # includes them); fall back to the viewed chat only if absent.
            res_chat_id = notification.get("chat_id") or self.chat_id
            res_sid = notification.get("session_id") or self.session_id
            delegate_event_data = json.dumps({
                "task_id": task_id,
                "task_name": task_name,
                "agent": delegate_agent,
                "output_text": output_preview,
                "status": result_status,
            })
            # Save the delegate_result event on the DELEGATING chat.
            if res_chat_id:
                task_store.add_chat_message(res_chat_id, "event", "",
                    event_type=wire.DELEGATE_RESULT,
                    event_data=delegate_event_data)
            # Notify the frontend (delegate block status + result) — only when
            # this socket is viewing the delegating chat.
            if res_chat_id == self.chat_id:
                await self._send({
                    "type": wire.DELEGATE_RESULT,
                    "task_id": task_id,
                    "task_name": task_name,
                    "agent": delegate_agent,
                    "output_text": output_preview,
                    "status": result_status,
                })
            # Drive the synthesis turn on the delegating chat (headless if unviewed).
            res_layer = self.layer if res_chat_id == self.chat_id else await self._resolve_layer_for_chat_async(res_chat_id)
            turn_pump = await self._run_server_turn(
                result_prompt,
                target_session_id=res_sid,
                target_chat_id=res_chat_id,
                target_layer=res_layer,
            )
            if turn_pump is None and res_chat_id and result_prompt:
                # No turn ran (dead target that couldn't warm, layer gone) —
                # the event row above is only a bubble. Park the prompt as a
                # durable wake so the next warmup/turn on the chat replays it
                # (the _start_new_stream chokepoint claims pending wakes).
                stored = task_store.append_pending_delegate_wake(
                    res_chat_id, result_prompt)
                logger.warning(
                    f"WS dashboard: delegate-result turn could not start for "
                    f"chat={res_chat_id[:8]} — wake "
                    f"{'stored for replay' if stored else 'NOT stored'}"
                )

        elif ntype == wire.NOTIFY_CONTINUATION_PROMPT:
            # A scheduled self-continuation woke this session's chat (scheduler
            # _fire_continuation). Mirror of task_result_prompt without the
            # delegate frames: persist the wake event on the ORIGINATING chat,
            # then drive the wake turn there (headless if unviewed).
            wake_chat_id = notification.get("chat_id") or self.chat_id
            wake_sid = notification.get("session_id") or self.session_id
            wake_prompt = notification.get("prompt", "")
            if wake_chat_id:
                task_store.add_chat_message(wake_chat_id, "event", "",
                    event_type=wire.PERSISTED_SCHEDULE_WAKE,
                    event_data=json.dumps({
                        "prompt": wake_prompt,
                        "task_id": notification.get("task_id", ""),
                    }))
            wake_layer = self.layer if wake_chat_id == self.chat_id else await self._resolve_layer_for_chat_async(wake_chat_id)
            await self._run_server_turn(
                wake_prompt,
                target_session_id=wake_sid,
                target_chat_id=wake_chat_id,
                target_layer=wake_layer,
            )

        else:
            logger.warning(f"WS dashboard: unknown notify item type={ntype!r} dropped")

    async def _run_server_turn(self,
        prompt: str, *, target_session_id: str | None, target_chat_id: str,
        target_layer: "ExecutionLayer | None", images: list[dict] | None = None,
        force_headless: bool = False,
    ) -> object | None:
        """Run a server-initiated turn (bg nudge / delegate result / server-kick)
        on its ORIGINATING chat, regardless of what this socket is viewing.

        The pump persists to the DB headless, so the result always lands in
        ``target_chat_id``. We stream to THIS socket only when it is viewing the
        target — preserving the single-socket-reader invariant (we run in the
        main-loop context here, never a background task). A turn on a non-viewed
        chat runs autonomously; we arm its bg monitor on completion. A dead,
        non-viewed target is WARMED headless inside _start_new_stream so
        the review/synthesis runs now; only a warm FAILURE returns None, leaving
        the persisted marker for when the chat is reopened.

        ``images`` (Direct LLM vision blocks) is forwarded for the first
        server-kicked turn that carried chat-attached photos. ``force_headless``
        runs the turn headless even when the target IS the viewed chat — used
        when the WS is gone (no socket to stream to, no main loop to attach)."""
        is_viewed = target_chat_id == self.chat_id and not force_headless
        pump = await self._start_new_stream(
            prompt,
            target_session_id=target_session_id,
            target_chat_id=target_chat_id,
            target_layer=target_layer,
            images=images,
        )
        if not pump:
            # Warm FAILED (a dead non-viewed target that couldn't resume), or a
            # genuine failure on the viewed chat → reset the viewed UI. Returns
            # None so the caller can store a durable wake — the persisted event
            # row alone is just a bubble, NOT a replayable prompt.
            if is_viewed:
                await self._send({"type": wire.DONE, "chat_id": self.chat_id or ""})
            return None
        if is_viewed:
            # A server-initiated turn (bg-nudge review / delegate-result synthesis)
            # has no user-send to flip the frontend's `streaming` state, so without
            # this it streams text but shows no generating timer and leaves Send
            # un-toggled (the turn can't be stopped). Tell the viewed socket a turn
            # is now active; the existing `done`/`aborted` events clear it.
            await self._send({"type": wire.SERVER_TURN_START, "chat_id": target_chat_id})
            # Viewed chat — stream to this socket (arms _ensure_bg_monitor at end).
            await self._enter_pump_loop()
        else:
            # Headless turn on a non-viewed chat — runs to the DB on its own;
            # the pump broadcasts its own chat_status start/end, and
            # _start_new_stream already armed its bg-monitor watcher.
            pass
        return pump

    async def _run_kick_headless(self,
        wcid: str, sid: str | None, text: str,
        images: list[dict], files: list[dict], *, force_headless: bool = False,
    ) -> None:
        """Run a backgrounded warmup's first turn HEADLESS on its own chat when
        the user navigated away during the spawn (or the WS is gone, via
        force_headless). Resolves the TARGET chat's agent context (not the
        viewed attributes) so attachments save to the right workspace, then
        drives the turn into wcid's pump + DB. The prompt was already persisted
        at send-time (_persist_first_prompt)."""
        from storage.pg import run_db
        rec = await run_db(task_store.get_chat, wcid)
        if not rec:
            return
        k_agent = rec.get("agent") or ""
        k_layer = await self._resolve_layer_for_chat_async(wcid)
        if not k_layer:
            logger.warning(
                f"WS dashboard: headless server-kick for chat {wcid} could not "
                f"resolve a layer (chat gone or pinned remote offline) — deferring"
            )
            return
        k_is_agent_scoped = _vis.is_shared_only(k_agent)
        k_agent_dir = config.get_agent_dir(k_agent)
        k_username = self.user.get("username") or ""
        # Per session (sid may still be None here: the placement default,
        # False, is right — an inline-images engine is never remote).
        k_is_direct = bool(k_layer.capabilities_for(sid or "").behaviour.attach_images_inline)
        from ws.dashboard_chat_support import AttachmentsRefused
        try:
            cli_text, attached_images, _meta, _vfiles = await self._process_attachments(
                text, images, files,
                agent=k_agent, agent_dir=k_agent_dir,
                is_agent_scoped=k_is_agent_scoped, username=k_username,
                is_direct_llm=k_is_direct,
            )
        except AttachmentsRefused as e:
            await self._send_error(str(e))
            return
        await self._run_server_turn(
            cli_text,
            target_session_id=sid,
            target_chat_id=wcid,
            target_layer=k_layer,
            images=attached_images or None,
            force_headless=force_headless,
        )
