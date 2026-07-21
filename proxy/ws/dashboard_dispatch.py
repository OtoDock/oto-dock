"""The between-turns client-message dispatcher (the in-stream twin lives in
ChatController._stream_via_pump).

ClientMessageDispatcher is a mixin of ``DashboardConnection`` (ws/dashboard.py) — methods run
with the connection's full attribute state; nothing here is standalone.
Behavior is pinned by tests/session/test_ws_dashboard_*.
"""

import asyncio
import contextlib
import base64
import logging
import config
from storage import database as task_store, notification_store
from services.notifications import notification_manager
from core.session.session_state import (
    set_session_mode,
    resolve_permission,
    resolve_question,
    resolve_location,
    set_user_tz,
    set_session_user_tz,
    clear_session_liveness,
)
from core.session.session_manager import resolve_execution_path
from core.events.stream_pump import _active_pumps, _pending_permissions
from core.session import visibility as _vis, interactive_session

logger = logging.getLogger("claude-proxy")


class ClientMessageDispatcher:
    """The between-turns client-message dispatcher (the in-stream twin lives
    in ChatController._stream_via_pump)."""

    async def _dispatch_client_message(self, msg: dict) -> str | None:

        msg_type = msg.get("type", "")

        if msg_type == "pre_warmup":
            # Background so the dispatcher can keep processing the user's
            # next click — the FIRST chat-history click after switching to a
            # remote agent was waiting 5–10s for the eager pre_warmup's
            # satellite-side session start + MCP sync to finish in front of
            # it. Awaiting in-flight pre_warmup is handled in _handle_warmup
            # (so the first send_message still reuses the pre-warmed session).
            if self._pre_warmup_task and not self._pre_warmup_task.done():
                self._pre_warmup_task.cancel()
            self._pre_warmup_task = asyncio.create_task(self._handle_pre_warmup(msg))
        elif msg_type == "warmup":
            prev_sid = self.session_id
            await self._handle_warmup(msg)
            # Same-chat warm: a fresh sid supersedes the previous one.
            self._register_notify_queue(replaces=prev_sid)
            count = await asyncio.to_thread(notification_store.get_unread_count, self.user_sub)
            await self._send({"type": "notification_count", "count": count})
        elif msg_type == "chat":
            if self.streaming:
                # Queue the message
                text = msg.get("text", "")
                if text:
                    self.message_queue.append(text)
                    await self._send({"type": "queued", "index": len(self.message_queue) - 1, "text": text})
            else:
                await self._handle_chat(msg)
        elif msg_type == "artifact_interaction":
            # display_ui backchannel send (idle → framed turn; streaming →
            # queued to the boundary). Validation + acks live in the handler.
            await self._handle_artifact_interaction(msg)
        elif msg_type == "app_action":
            # Pinned mini-app send_prompt action — same delivery rails,
            # gated on the user-approved manifest instead of a chat-bound
            # capability token.
            await self._handle_app_action(msg)
        elif msg_type == "resume_chat":
            await self._handle_resume_chat(msg)
            self._register_notify_queue()
            # If the resumed chat has an active pump, attach to it
            await self._enter_pump_loop()
            # Reconnected mid-background-run (turn already ended, bg subagents
            # still finishing) → relaunch the monitor so the review nudge still
            # fires. Idempotent: a no-op if one is already running or none pending.
            self._ensure_bg_monitor()
        elif msg_type == "chat_read":
            await self._handle_chat_read(msg)
        elif msg_type == "permission_response":
            # Resolve hook-based permission (unblocks the long-poll in hook endpoint).
            # Handled both mid-stream (in _stream_via_pump) and between turns (here).
            # dual-control: while a local `otodock` terminal is the active
            # controller, the human answers permissions in the native TUI — drop a
            # (stale) dashboard response so it can't advance the agent out from
            # under them.
            _ds_isess = interactive_session.get(self.session_id) if self.session_id else None
            if _ds_isess is not None and _ds_isess.otodock_attached:
                pass
            elif await self._may_resolve_permission(msg["request_id"]):
                resolve_permission(msg["request_id"], msg.get("approved", True))
            else:
                return None
            for sid, pd in list(_pending_permissions.items()):
                if pd.get("request_id") == msg["request_id"]:
                    del _pending_permissions[sid]
                    break
        elif msg_type == "question_response":
            # Codex request_user_input answer arriving between turns (safety net;
            # a held question normally resolves mid-stream in _stream_via_pump).
            if await self._may_resolve_permission(msg["request_id"]):
                resolve_question(msg["request_id"], msg.get("answers") or {})
                for sid, pd in list(_pending_permissions.items()):
                    if pd.get("request_id") == msg["request_id"]:
                        del _pending_permissions[sid]
                        break
        elif msg_type == "pty_attach":
            # The client's terminal has mounted + subscribed → attach the PTY
            # viewer NOW (the scrollback replay can't race the subscribe). Resolve
            # the connection's viewed interactive session; guard the chat matches
            # so a fast chat-switch can't attach the wrong terminal.
            isess = interactive_session.get(self.session_id) if self.session_id else None
            if isess is not None and isess.alive and isess.chat_id == msg.get("chat_id"):
                await self._attach_pty_viewer(isess)
        elif msg_type == "pty_input":
            # Interactive (PTY) keystrokes → the connection's VIEWED session
            # (session_id, set on warmup_ready). Routing by session_id (not the
            # attach-set _pty_viewer_sid) means the cold-start first input still
            # lands before the pty_attach handshake. get() returns None for a
            # headless session → no-op. write_input resets the idle timer.
            isess = interactive_session.get(self.session_id) if self.session_id else None
            if isess is not None:
                try:
                    isess.deliver_dashboard_input(
                        base64.b64decode(msg.get("data", "")),
                        composer=bool(msg.get("composer")),
                    )
                except Exception:
                    logger.debug("pty_input decode/write failed", exc_info=True)
        elif msg_type == "pty_resize":
            # Client terminal resize → SIGWINCH to the TUI (viewed session).
            isess = interactive_session.get(self.session_id) if self.session_id else None
            if isess is not None:
                try:
                    isess.resize(int(msg.get("rows", 24)), int(msg.get("cols", 80)))
                except Exception:
                    logger.debug("pty_resize failed", exc_info=True)
        elif msg_type == "pty_attachments":
            # Interactive (PTY) photo/file attachments.
            # Reuse the normal-turn attachment pipeline: save base64 photos to the
            # agent's scope-correct workspace (+ push to any remote satellite) and
            # build the prompt with sandbox-virtual paths the TUI's Read tool can
            # open, then type it into the live PTY (bracketed paste so the
            # multi-line path block lands as one input) and submit with Enter.
            isess = interactive_session.get(self.session_id) if self.session_id else None
            if isess is not None and isess.alive:
                try:
                    is_agent_scoped = _vis.is_shared_only(self.agent_name)
                    agent_dir = config.get_agent_dir(self.agent_name)
                    username = self.user.get("username") or ""
                    cli_text, _imgs, _meta, _vfiles = await self._process_attachments(
                        msg.get("text", ""), msg.get("images", []) or [], msg.get("files", []) or [],
                        agent=self.agent_name, agent_dir=agent_dir,
                        is_agent_scoped=is_agent_scoped, username=username, is_direct_llm=False,
                    )
                    payload = "\x1b[200~" + cli_text + "\x1b[201~\r"
                    # Attachment sends only ever come from the composer — same
                    # question-parked hold as flagged pty_input.
                    isess.deliver_dashboard_input(payload.encode("utf-8"), composer=True)
                except Exception:
                    logger.exception("pty_attachments failed")
        elif msg_type == "location_response":
            resolve_location(msg["request_id"], {
                "lat": msg.get("lat"),
                "lng": msg.get("lng"),
                "accuracy": msg.get("accuracy"),
                "error": msg.get("error"),
            })
        elif msg_type == "plan_review_response":
            # Plan review between turns — can happen normally or after session death
            # dual-control: ignore a dashboard plan-review response while a local
            # `otodock` terminal controls the session (it reviews in the native TUI).
            _ds_isess = interactive_session.get(self.session_id) if self.session_id else None
            if _ds_isess is not None and _ds_isess.otodock_attached:
                return None
            if not await self._may_resolve_permission(msg["request_id"]):
                return None
            action = msg.get("action", "")
            plan_fn = msg.get("filename", "")
            approved = action != "edit"  # approve for implement + reject (cancel)
            # Set session mode BEFORE resolve_permission (same race fix as streaming path)
            if approved and self.session_id:
                if action == "reject":
                    set_session_mode(self.session_id, self.pre_plan_mode_holder[0])
                elif action == "implement_accept_edits":
                    set_session_mode(self.session_id, "acceptEdits")
                elif action == "implement_default":
                    set_session_mode(self.session_id, "default")
            resolve_permission(msg["request_id"], approved)
            for sid, pd in list(_pending_permissions.items()):
                if pd.get("request_id") == msg["request_id"]:
                    del _pending_permissions[sid]
                    break
            if self.chat_id and plan_fn and action == "reject":
                task_store.update_chat_plan_status(self.chat_id, plan_fn, "rejected")
                # Restore pre-plan mode + notify frontend
                restored_mode = self.pre_plan_mode_holder[0]
                task_store.update_chat(self.chat_id, permission_mode=restored_mode)
                await self._send({"type": "mode_changed", "mode": restored_mode})
            if action == "implement_accept_edits":
                await self._handle_mode_change({"mode": "acceptEdits"})
                self.implementing_plan = plan_fn
                # If session is dead (stale plan_review), queue implement for after warmup
                if not self.session_id:
                    self.message_queue.append("Please implement the plan now.")
                    logger.info(f"WS dashboard: queued implement message for dead session, plan={plan_fn}")
            elif action == "implement_default":
                await self._handle_mode_change({"mode": "default"})
                self.implementing_plan = plan_fn
                if not self.session_id:
                    self.message_queue.append("Please implement the plan now.")
                    logger.info(f"WS dashboard: queued implement message for dead session, plan={plan_fn}")
        elif msg_type == "mode_change":
            await self._handle_mode_change(msg)
        elif msg_type == "model_change":
            await self._handle_model_change(msg)
        elif msg_type == "execution_mode_change":
            await self._handle_execution_mode_change(msg)
        elif msg_type == "execution_mode_switch":
            await self._handle_switch_execution_mode(msg)
        elif msg_type == "compact_context":
            await self._handle_compact_context()
        elif msg_type == "move_chat":
            await self._handle_move_chat()
        elif msg_type == "implement_plan":
            await self._handle_implement_plan(msg)
            self._register_notify_queue()
        elif msg_type == "abort":
            # Abort-during-spawn: a backgrounded warmup is still
            # spawning the session. Do NOT cancel the spawn — cancelling a
            # half-started CLI/satellite process can't reliably stop it (codex/
            # claude keep running and then answer the server-kicked first turn).
            # Instead flag the chat: _spawn_tail finishes the spawn, then kills
            # the session + suppresses warmup_ready/kick (and the _server_kick
            # handler covers the race where the spawn already enqueued the kick).
            # Tell the client to drop "Getting ready" now.
            if self._warmup_task and not self._warmup_task.done():
                self._warmup_abort_chat = self.chat_id
                await self._send({"type": "aborted", "chat_id": self.chat_id or ""})
                return None
            # Interactive PTY chat: the layer/pump machinery below is
            # headless-only (PTY sessions live in interactive_session._sessions,
            # not the layer registries) — Stop means "press ESC in the TUI",
            # both CLIs' native stop-generation key. No abort flags are stamped
            # (the cancelled-context re-inject is headless machinery; the TUI
            # keeps its partial turn natively) and liveness stays: the process
            # survives. The turn state closes via the transcript interrupt
            # markers → chat_status ready.
            isess = interactive_session.get(self.session_id) if self.session_id else None
            if isess is not None and isess.alive:
                isess.interrupt_turn()
                await self._send({"type": "aborted", "chat_id": self.chat_id or ""})
                return None
            # Non-streaming abort: no attached pump, but a detached pump may be
            # mid-turn and the process may be running. Layer abort FIRST — the
            # graceful path (Claude interrupt / Codex turn/interrupt) keeps the
            # producer alive so the detached pump persists the partial turn;
            # the hard path cancels it as before (see _stream_via_pump's twin).
            self.implementing_plan = ""
            graceful = False
            if self.session_id and self.layer:
                graceful = bool(await self.layer.abort(self.session_id))
            pump = _active_pumps.get(self.chat_id)
            if pump and not pump.is_done:
                _dropped_q = pump.cancel_all_queued()
                if _dropped_q:
                    await self._send({"type": "queue_cleared", "text": _dropped_q})
                if not graceful:
                    pump.abort()
            # The connection's own between-turns queue dies with the abort too
            # (pending artifact interactions included — never delivered, never
            # persisted).
            self.artifact_queue.clear()
            if self.message_queue:
                _dropped_c = "\n\n".join(self.message_queue)
                self.message_queue.clear()
                await self._send({"type": "queue_cleared", "text": _dropped_c})
            if self.session_id:
                _pending_permissions.pop(self.session_id, None)
            # last_turn_aborted feeds the scheduler's user_interrupted on every
            # abort path; the graceful flag suppresses only the next turn's
            # cancelled-context injection (engine history kept the partial turn).
            if self.chat_id:
                task_store.update_chat(self.chat_id, last_turn_aborted=True,
                                       last_abort_graceful=graceful)
                # A hard Claude CLI Stop kills the whole process group —
                # background agents/commands died with it and can never emit
                # their own clears. Graceful keeps the process alive; a Codex
                # abort keeps the daemon (and its bg sub-agent threads) either
                # way, so its supervisor still owns those badges.
                if self.session_id and not graceful:
                    _abort_chat = task_store.get_chat(self.chat_id)
                    _abort_path = resolve_execution_path(
                        self.agent_name, (_abort_chat or {}).get("execution_path", ""),
                    )
                    if _abort_path == "claude-code-cli":
                        clear_session_liveness(self.session_id, reason="abort")
            await self._send({"type": "aborted", "chat_id": self.chat_id or ""})
        elif msg_type == "cancel_queued":
            idx = msg.get("index", -1)
            if 0 <= idx < len(self.message_queue):
                text = self.message_queue.pop(idx)
                await self._send({"type": "queue_removed", "index": idx, "text": text})
        elif msg_type == "cancel_all_queued":
            combined = "\n\n".join(self.message_queue) if self.message_queue else ""
            self.message_queue.clear()
            await self._send({"type": "queue_cleared", "text": combined})
        elif msg_type == "client_info":
            platform = msg.get("platform", "web")
            notification_manager.set_connection_platform(self.user_sub, self.notify_connection_id, platform)
            time_zone = msg.get("time_zone")
            if time_zone:
                set_user_tz(self.user_sub, time_zone)
                if self.session_id:
                    set_session_user_tz(self.session_id, time_zone)
            logger.info(
                f"WS dashboard client_info: user={self.user_sub}, platform={platform}, tz={time_zone or '-'}"
            )
        elif msg_type == "user_active":
            notification_manager.set_connection_active(self.user_sub, self.notify_connection_id, True)
        elif msg_type == "user_idle":
            notification_manager.set_connection_active(
                self.user_sub, self.notify_connection_id, False,
                away=bool(msg.get("away")),
            )
        elif msg_type == "ping":
            await self._send({"type": "pong"})
        elif msg_type == "close":
            logger.info(f"WS dashboard close: session={self.session_id}, chat={self.chat_id}")
            return "close"
        else:
            await self._send_error(f"Unknown message type: {msg_type}")
        return None

    async def _handle_move_chat(self):
        """Move the OPEN chat to the agent's CURRENT target — the locality
        escape hatch.

        Rebinds via the proven machine-removed shape: pin cleared, session
        ids dropped, ``pending_history_seed='moved:<from>'`` — the next
        warmup fresh-resolves, spawns on the current target and injects the
        DB-history digest. The old session is closed here (headless
        close_session releases slot + subscription; interactive PTYs are
        closed for the chat) so a warm slot never leaks; its on-disk state
        stays behind by design. Owner/admin only: shared-agent
        editors/viewers can hold a connection to this chat, but a
        non-owner's resolve is role-forced to 'local' and must never
        relocate someone else's chat."""
        from storage import remote_store
        from ws.dashboard import _effective_agent_role

        logger.info(
            "WS dashboard: move_chat requested chat=%s session=%s streaming=%s",
            self.chat_id or "?", (self.session_id or "?")[:8], self.streaming,
        )
        chat = task_store.get_chat(self.chat_id) if self.chat_id else None
        if not chat:
            await self._send({"type": "error", "message": "Chat not found."})
            return
        agent = chat.get("agent") or ""
        role = _effective_agent_role(self.user_sub, agent)
        if chat.get("user_sub") != self.user_sub and role != "admin":
            await self._send({"type": "error",
                              "message": "Only the chat owner can move it."})
            return
        if self._warmup_task and not self._warmup_task.done():
            await self._send({"type": "error",
                              "message": "The chat is still getting ready — "
                                         "try again in a moment."})
            return
        pump = _active_pumps.get(self.chat_id) if self.chat_id else None
        if self.streaming or (pump and not pump.is_done):
            await self._send({"type": "error",
                              "message": "Finish or stop the current turn "
                                         "before moving the chat."})
            return
        isess = (interactive_session.get(self.session_id)
                 if self.session_id else None)
        if isess is not None and isess.alive and isess.turn_open:
            await self._send({"type": "error",
                              "message": "Finish or stop the current turn "
                                         "before moving the chat."})
            return

        pin = chat.get("execution_target") or ""
        resolved, _ = remote_store.resolve_execution_target(
            agent, self.user_sub, role,
        )
        if not resolved or resolved.startswith("__offline__:"):
            await self._send({"type": "error",
                              "message": "The agent's current machine is "
                                         "offline — nowhere to move to."})
            return
        if not pin or pin == resolved:
            await self._send({"type": "error",
                              "message": "This chat already runs on the "
                                         "agent's current target."})
            return

        def _label(target: str) -> str:
            if target == "local":
                return "the local sandbox"
            m = remote_store.get_remote_machine(target) or {}
            return str(m.get("name") or "") or target[:8]

        from_label = _label(pin)
        # Close the old session FIRST (idle by the gates above): headless
        # close releases the concurrency slot + subscription binding; the
        # session file / satellite-side state stays behind by design.
        if isess is not None and isess.alive:
            with contextlib.suppress(Exception):
                await interactive_session.close_for_chat(
                    self.chat_id, reason="moved")
        elif self.session_id and self.layer:
            try:
                await self.layer.close_session(self.session_id)
            except Exception:
                logger.warning("move_chat: close_session failed for %s",
                               self.session_id, exc_info=True)

        if not remote_store.rebind_chat_to_current_target(
                self.chat_id, from_label):
            await self._send({"type": "error", "message": "Move failed."})
            return
        self.session_id = None
        logger.info(
            "WS dashboard: chat %s moved (%s -> %s) by %s",
            self.chat_id, pin, resolved, self.user_sub,
        )
        await self._send({
            "type": "chat_moved",
            "chat_id": self.chat_id,
            "new_target": resolved,
            "resolved_label": _label(resolved).removeprefix("the "),
        })

    async def _handle_compact_context(self):
        """Manual context compaction — Codex ``thread/compact/start`` via
        ``layer.compact()``. Claude headless has no compaction channel (the
        CLI does not execute /compact from stream-json user frames — tested
        on 2.1.201), so its layer returns None and the button is hidden for
        it anyway. Between turns only: the connection's own streaming flag is
        connection-scoped, so a DETACHED background pump mid-turn must also
        block. The handler owns the event transport — no pump exists while
        idle, so it sends CONTEXT_COMPACT frames itself and persists the
        completed block exactly like the pump would."""
        import json as _json
        # Always log the click — a silent refuse path made a remote-codex
        # compact no-op look like the frame never reached the backend.
        logger.info(
            "WS dashboard: manual compact requested chat=%s session=%s "
            "layer=%s streaming=%s",
            self.chat_id or "?", (self.session_id or "?")[:8],
            type(self.layer).__name__ if self.layer else None, self.streaming,
        )
        if not (self.session_id and self.layer):
            # Lazy-resumed chat with no live session yet (e.g. right after a
            # proxy restart) — a silent no-op reads as a broken button.
            await self._send({"type": "error",
                              "message": "No active session — send a message "
                                         "first, then compact."})
            return
        pump = _active_pumps.get(self.chat_id) if self.chat_id else None
        if self.streaming or (pump and not pump.is_done):
            await self._send({"type": "error",
                              "message": "Cannot compact while a turn is running."})
            return
        await self._send({"type": "context_compact", "phase": "started",
                          "trigger": "manual", "chat_id": self.chat_id or ""})
        result = None
        try:
            async with self.layer.session_lock(self.session_id):
                result = await self.layer.compact(self.session_id)
        except Exception:
            logger.exception(
                f"WS dashboard: compaction failed, session={self.session_id}"
            )
        if result is None:
            await self._send({"type": "context_compact", "phase": "failed",
                              "chat_id": self.chat_id or ""})
            await self._send({"type": "error",
                              "message": "Context compaction failed or is not "
                                         "supported by this engine."})
            return
        post_tokens = result.get("post_tokens")
        evt = {"type": "context_compact", "phase": "completed",
               "trigger": "manual", "post_tokens": post_tokens}
        if self.chat_id:
            # Persist the separator block (pump-shaped event row) and pin the
            # gauge so a reload shows the compacted size.
            task_store.add_chat_message(self.chat_id, "event", "",
                                        event_data=_json.dumps(evt))
            if post_tokens is not None:
                task_store.update_chat(self.chat_id, context_used=post_tokens)
        await self._send({**evt, "chat_id": self.chat_id or ""})
