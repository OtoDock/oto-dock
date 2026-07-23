"""Server-initiated work delivered via the notify queue: notification forwards,
the server-kicked first turn, and bg-nudge / task-result review turns on their
originating chats.

ServerNotificationController is a mixin of ``DashboardConnection`` (ws/dashboard.py) — methods run
with the connection's full attribute state; nothing here is standalone.
Behavior is pinned by tests/session/test_ws_dashboard_*.
"""

import json
import logging
import config
from storage import database as task_store
from core.session import visibility as _vis

logger = logging.getLogger("claude-proxy")


class ServerNotificationController:
    """Server-initiated work delivered via the notify queue: notification
    forwards, the server-kicked first turn, and bg-nudge / task-result
    review turns on their originating chats."""

    async def _handle_server_notification(self, notification: dict):
        """Process a server-initiated notification (bg nudge, task result, agent done)."""
        ntype = notification.get("type", "")

        if ntype == "notification":
            # Forward notification delivery to frontend for toast/bell
            await self._send(notification)
            return

        if ntype == "notification_silent":
            # Silent inbox/badge update — fired on inactive WS connections by
            # notification_manager._deliver_to_user. Frontend's onNotificationSilent
            # adds the delivery to the inbox + bumps the badge without showing a
            # toast or playing sound. Native push handles the actual alert on
            # whichever device is currently engaged (or no device, if all are idle).
            await self._send(notification)
            return

        if ntype == "file_updated":
            # A shared workspace file changed (a Collabora save
            # or an agent/disk write). Forward to the frontend so an open
            # Collabora preview / workspace file-tree can refresh. Carries
            # agent_slug, rel_path, source ("collabora"|"disk"); the client
            # decides whether to reload (dirty-guarded) and ignores files it
            # doesn't currently have open.
            await self._send(notification)
            return

        if ntype in ("satellite_updating", "satellite_updated", "satellite_update_failed"):
            # In-flight satellite update lifecycle. Pushed by
            # proxy/ws/satellite.py helpers when version-mismatched
            # satellites get the new tarball, reconnect on the new
            # version, or fail and roll back. Frontend dispatches into
            # machineUpdateStore which drives the MachineUpdateBanner.
            await self._send(notification)
            return

        if ntype.startswith("install_") or ntype == "mcp_install_failed":
            # MCP-install lifecycle (install_started / install_mcp_plan /
            # install_progress / install_heartbeat / install_verifying /
            # install_done / install_failed / mcp_install_failed). Pushed by
            # core/install_registry via ws/satellite.py::push_install_event to
            # every dashboard tab of the machine's owner (+ admins), so the
            # install bar shows live regardless of which connection triggered
            # the warmup. Frontend routes these into useInstallStore (keyed by
            # machine_id::agent). Forward verbatim.
            await self._send(notification)
            return

        if ntype == "turn_complete":
            # Forward turn-complete signal to frontend for subtle ping sound
            await self._send(notification)
            return

        if ntype == "chat_status":
            # Per-chat live-dot signal (pump turn start/end, broadcast to every
            # connection of the chat owner) — forward so the sidebar dot
            # lights/clears for chats generating in the background. The notify
            # queue is only drained BETWEEN the viewed chat's turns, so a
            # turn-START "streaming" can sit queued for a whole turn and land
            # right after `done` — where the frontend's auto-attach reads it as
            # a fresh turn and pointlessly re-resumes the chat (a full-history
            # reload flash at every turn end). Drop a "streaming" whose chat no
            # longer has a live turn; its matching "ready" is right behind.
            if notification.get("status") == "streaming":
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

        if ntype == "chat_read":
            # Someone opened the chat (another tab of this user, or any user
            # of a shared-only chat) — forward verbatim so the sidebar unread
            # dot clears everywhere without a refetch.
            await self._send(notification)
            return

        if ntype == "goal_update":
            # Post-turn codex thread-goal change (goal accounting at turn stop
            # lands after turn/completed — no pump to carry it). Forward
            # verbatim; the frontend's per-chat gate scopes it to the viewer.
            await self._send(notification)
            return

        if ntype == "bg_agent_done":
            # Individual subagent completed — forward to frontend so it clears
            # that one widget by tool_use_id (order-independent, no FIFO).
            await self._send({"type": "bg_agent_done",
                         "tool_use_id": notification.get("tool_use_id", "")})
            return

        if ntype == "location_request":
            # Forward location request to dashboard (fallback when no pump active)
            await self._send(notification.get("data", notification))
            return

        if ntype == "_server_kick":
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
                _k_layer = self.layer if kick_cid == self.chat_id else self._resolve_layer_for_chat(kick_cid)
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
        if ntype == "bg_nudge":
            count = notification["count"]
            # Originating chat/session from the notification (set by the bg
            # monitor, stream_pump.py); fall back to the viewed chat only if
            # absent (shouldn't happen — the monitor always carries them).
            nudge_chat_id = notification.get("chat_id") or self.chat_id
            nudge_sid = notification.get("session_id") or self.session_id
            nudge = f"Your {count} background agent(s) have completed. Please review the results and continue."
            # Save the system event on the ORIGINATING chat.
            if nudge_chat_id:
                task_store.add_chat_message(nudge_chat_id, "event", "",
                    event_type="bg_nudge", event_data=json.dumps({"count": count}))
            # Clear the bg-agent status bar — only if this socket is viewing the
            # originating chat (else it would clear the WRONG chat's badges; a
            # reattach reconstructs the state from live_state/chat_history).
            if nudge_chat_id == self.chat_id:
                await self._send({"type": "bg_agents_complete", "count": count})
            # Drive the review turn on the originating chat (headless if unviewed).
            nudge_layer = self.layer if nudge_chat_id == self.chat_id else self._resolve_layer_for_chat(nudge_chat_id)
            await self._run_server_turn(
                nudge,
                target_session_id=nudge_sid,
                target_chat_id=nudge_chat_id,
                target_layer=nudge_layer,
            )

        elif ntype == "bg_command_nudge":
            # Background bash command(s) finished post-turn (the bg-command
            # monitor in stream_pump.py). Drives the review turn AND clears the
            # badges — mirror of bg_nudge with command wording.
            count = notification["count"]
            nudge_chat_id = notification.get("chat_id") or self.chat_id
            nudge_sid = notification.get("session_id") or self.session_id
            nudge = (f"Your {count} background command(s) have finished. "
                     f"Review their output and continue with the task.")
            if nudge_chat_id:
                task_store.add_chat_message(nudge_chat_id, "event", "",
                    event_type="bg_command_nudge", event_data=json.dumps({"count": count}))
            # Clear the bg-command badges on the viewed socket. The per-command
            # bg_command_done can't deliver post-turn (the turn's pump is gone —
            # app.py _push_to_pump needs an active pump), so this notify-queue
            # path is the reliable clear, exactly like bg_nudge → bg_agents_complete.
            if nudge_chat_id == self.chat_id:
                await self._send({"type": "bg_commands_complete", "count": count})
            nudge_layer = self.layer if nudge_chat_id == self.chat_id else self._resolve_layer_for_chat(nudge_chat_id)
            await self._run_server_turn(
                nudge,
                target_session_id=nudge_sid,
                target_chat_id=nudge_chat_id,
                target_layer=nudge_layer,
            )

        elif ntype == "liveness_clear":
            # A session died with liveness still showing (its own lifecycle
            # events can no longer clear the badges — clear_session_liveness
            # broadcast this). Emit the cohort-clear frames if this socket is
            # viewing the affected chat. No server turn, no persistence: the
            # session is dead, and a DB reload already renders history blocks
            # inactive — only the live view needs the clears.
            dead_chat = notification.get("chat_id", "")
            if dead_chat and dead_chat == self.chat_id:
                await self._send({"type": "bg_agents_complete", "count": 0})
                await self._send({"type": "bg_commands_complete", "count": 0})
                await self._send({"type": "fg_agents_complete"})
                logger.info(
                    f"WS dashboard: cleared liveness badges for chat={dead_chat[:8]} "
                    f"(session death: {notification.get('reason') or '-'})"
                )

        elif ntype == "chat_ui_frame":
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

        elif ntype == "task_result_prompt":
            task_id = notification.get("task_id", "")
            task_name = notification["task_name"]
            result_prompt = notification["result_prompt"]
            delegate_agent = notification.get("delegate_agent", "")
            output_preview = notification.get("output_text", "")
            # completed | failed | cancelled | user_interrupted — drives the
            # badge icon; user_interrupted marks a lane the user stopped/steered.
            result_status = notification.get("status", "completed")
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
                    event_type="delegate_result",
                    event_data=delegate_event_data)
            # Notify the frontend (delegate block status + result) — only when
            # this socket is viewing the delegating chat.
            if res_chat_id == self.chat_id:
                await self._send({
                    "type": "delegate_result",
                    "task_id": task_id,
                    "task_name": task_name,
                    "agent": delegate_agent,
                    "output_text": output_preview,
                    "status": result_status,
                })
            # Drive the synthesis turn on the delegating chat (headless if unviewed).
            res_layer = self.layer if res_chat_id == self.chat_id else self._resolve_layer_for_chat(res_chat_id)
            await self._run_server_turn(
                result_prompt,
                target_session_id=res_sid,
                target_chat_id=res_chat_id,
                target_layer=res_layer,
            )

        elif ntype == "continuation_prompt":
            # A scheduled self-continuation woke this session's chat (scheduler
            # _fire_continuation). Mirror of task_result_prompt without the
            # delegate frames: persist the wake event on the ORIGINATING chat,
            # then drive the wake turn there (headless if unviewed).
            wake_chat_id = notification.get("chat_id") or self.chat_id
            wake_sid = notification.get("session_id") or self.session_id
            wake_prompt = notification.get("prompt", "")
            if wake_chat_id:
                task_store.add_chat_message(wake_chat_id, "event", "",
                    event_type="schedule_wake",
                    event_data=json.dumps({
                        "prompt": wake_prompt,
                        "task_id": notification.get("task_id", ""),
                    }))
            wake_layer = self.layer if wake_chat_id == self.chat_id else self._resolve_layer_for_chat(wake_chat_id)
            await self._run_server_turn(
                wake_prompt,
                target_session_id=wake_sid,
                target_chat_id=wake_chat_id,
                target_layer=wake_layer,
            )

    async def _run_server_turn(self,
        prompt: str, *, target_session_id: str | None, target_chat_id: str,
        target_layer: "ExecutionLayer | None", images: list[dict] | None = None,
        force_headless: bool = False,
    ) -> None:
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
            # Warm FAILED (a dead non-viewed target that couldn't resume — its
            # marker is already persisted, so it runs on reopen), or a genuine
            # failure on the viewed chat → reset the viewed UI.
            if is_viewed:
                await self._send({"type": "done", "chat_id": self.chat_id or ""})
            return
        if is_viewed:
            # A server-initiated turn (bg-nudge review / delegate-result synthesis)
            # has no user-send to flip the frontend's `streaming` state, so without
            # this it streams text but shows no generating timer and leaves Send
            # un-toggled (the turn can't be stopped). Tell the viewed socket a turn
            # is now active; the existing `done`/`aborted` events clear it.
            await self._send({"type": "server_turn_start", "chat_id": target_chat_id})
            # Viewed chat — stream to this socket (arms _ensure_bg_monitor at end).
            await self._enter_pump_loop()
        else:
            # Headless turn on a non-viewed chat — runs to the DB on its own;
            # the pump broadcasts its own chat_status start/end, and
            # _start_new_stream already armed its bg-monitor watcher.
            pass

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
        rec = task_store.get_chat(wcid)
        if not rec:
            return
        k_agent = rec.get("agent") or ""
        k_layer = self._resolve_layer_for_chat(wcid)
        if not k_layer:
            logger.warning(
                f"WS dashboard: headless server-kick for chat {wcid} could not "
                f"resolve a layer (chat gone or pinned remote offline) — deferring"
            )
            return
        k_is_agent_scoped = _vis.is_shared_only(k_agent)
        k_agent_dir = config.get_agent_dir(k_agent)
        k_username = self.user.get("username") or ""
        k_is_direct = bool(k_layer.capabilities.name == "direct-llm")
        cli_text, attached_images, _meta, _vfiles = await self._process_attachments(
            text, images, files,
            agent=k_agent, agent_dir=k_agent_dir,
            is_agent_scoped=k_is_agent_scoped, username=k_username,
            is_direct_llm=k_is_direct,
        )
        await self._run_server_turn(
            cli_text,
            target_session_id=sid,
            target_chat_id=wcid,
            target_layer=k_layer,
            images=attached_images or None,
            force_headless=force_headless,
        )
