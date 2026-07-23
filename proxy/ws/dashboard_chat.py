"""Chat turns + streaming: user sends, resume/history replay, the producer/pump
plumbing, permission gates, attachments, and the mode/model/execution-
mode/implement-plan controls.

ChatController is a mixin of ``DashboardConnection`` (ws/dashboard.py) — methods run
with the connection's full attribute state; nothing here is standalone.
Behavior is pinned by tests/session/test_ws_dashboard_*.
"""

import asyncio
import json
import logging
import re
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from fastapi import WebSocketDisconnect
import config
from storage import database as task_store, agent_store
from services.notifications import notification_manager
from core.session.session_state import (
    _chat_streaming_state,
    set_session_mode,
    get_session_mode,
    get_permission_queue,
    resolve_permission,
    resolve_question,
    resolve_location,
    get_permission_request_session,
    get_meeting_session_info,
    get_user_tz,
    set_session_user_tz,
    get_subagent_registry,
    clear_session_liveness,
)
from core.events.common_events import CommonEvent, ERROR, QUEUE_TURN, ARTIFACT_TURN, PRODUCER_DONE
from core.execution_layer import ExecutionLayer
from core.session.session_manager import get_execution_layer, resolve_execution_path
from core.session.history_seed import consume_pending_seed
from core.events.stream_pump import (
    ChatStreamPump,
    _active_pumps,
    _pending_permissions,
    _bg_agent_monitor,
    bg_monitor_running,
    _bg_command_monitor,
    bg_command_monitor_running,
)
from core.events.bg_command_state import get_bg_command_registry
from core.session import warmup_registry, visibility as _vis, interactive_session
# Imported by ws/dashboard.py AFTER its helpers are defined —
# safe intra-unit circularity (see the class assembly there).
from ws.dashboard import (
    STALE_TURN_SECS,
    _CHAT_PAGE,
    _EXTERNAL_DRIVEN_SOURCES,
    _build_chat_restore,
    _effective_agent_role,
    _host_to_sandbox_path,
    _model_allowed_for_path,
    _save_base64_image,
    _task_continue_allowed,
)

logger = logging.getLogger("claude-proxy")

# Injected `[Current time: ...]` stamp line(s) — twin of
# ``transcript_tailer._TIME_PRELUDE_RE`` (start-anchored exact shape only).
_TIME_PRELUDE_RE = re.compile(r"^\[Current time: [^\]\n]{1,160}\][ \t]*(?:\r?\n+|$)")

# A mini-app action's framed prompt header (``ws/artifact_interactions.
# frame_text`` — title/label have had '"' replaced with "'"). Recognized by
# the title chokepoints so an action-started chat names as "App — Label",
# never the raw framing brackets. Twin in ``transcript_tailer``.
_APP_ACTION_HEADER_RE = re.compile(
    r'^\[action from mini-app "(.{1,200}?)" — (.{1,80}?)\]'
)


class ChatController:
    """Chat turns + streaming: user sends, resume/history replay, the
    producer/pump plumbing, permission gates, attachments, and the
    mode/model/execution-mode/implement-plan controls."""

    async def _handle_chat(self, msg: dict):

        text = msg.get("text", "")
        images = msg.get("images", [])  # [{data: "data:image/...;base64,...", name: "photo.jpg"}, ...]
        files = msg.get("files", [])    # [{path: "users/{username}/workspace/...", name: "report.pdf"}, ...]
        msg_chat_id = msg.get("chat_id", "")
        # Server-kicked first turn: the prompt was already persisted
        # + titled in _do_warmup at send-time, so skip the re-persist here and
        # just run the turn. Set by _handle_warmup when warmup carried the prompt.
        server_kick = bool(msg.get("_server_kick"))
        # Artifact-backchannel turn: text is the framed interaction and the
        # distinct artifact_interaction row was already persisted by
        # _handle_artifact_interaction — never a "user" row, never a title.
        artifact_framed = bool(msg.get("_artifact_framed"))

        if not text and not images and not files:
            await self._send_error("Empty message")
            return

        # Route the end-of-turn alert to the device that sent this prompt (the
        # server-kicked first turn already recorded it at _persist_first_prompt).
        if not server_kick:
            notification_manager.set_chat_turn_origin(
                self.user_sub, msg_chat_id or self.chat_id or "", self.notify_connection_id,
            )

        # Task continue-gate (security boundary): a turn must never be driven
        # on a task this user can't continue — agent-scoped → editor+,
        # user-scoped → creator/admin. Checks the chat being driven (the
        # message's chat_id, or the WS's current chat_id on the normal path).
        if await self._deny_task_continue(msg_chat_id or self.chat_id):
            return

        # Self-heal: if WS state is missing (post-reconnect race where `chat`
        # arrives before `resume_chat`, or `resume_chat` was never sent — common
        # on Android after a screen off/on cycle if the visibility handler missed
        # the dead-WS health check), restore agent_name/chat_id/layer from the
        # DB using the chat_id the client sent. Without this, the auto-warmup
        # below is skipped and the user sees "No session — send warmup first".
        if msg_chat_id and (not self.chat_id or not self.agent_name):
            chat = task_store.get_chat(msg_chat_id)
            if chat:
                allowed = chat["user_sub"] == self.user_sub or self.user_role == "admin"
                if not allowed:
                    chat_agent = chat.get("agent", "")
                    is_assigned = chat_agent in self.user_agents
                    is_agent_scoped = (
                        _vis.is_shared_only(chat_agent)
                        or chat.get("source_type") == "phone"
                        or _vis.is_shared_chat_owner(chat["user_sub"])
                    )
                    allowed = is_assigned and is_agent_scoped
                if allowed:
                    # In-flight warmup re-attach: if a warmup is still running
                    # for this chat (fresh-satellite MCP install in progress),
                    # attach our WS as a listener + replay history so the UI
                    # catches up. The eventual warmup_ready reaches us via the
                    # registry; the user will need to re-send this message
                    # after the session is ready (frontend store handles that).
                    inflight = warmup_registry.get(msg_chat_id)
                    if inflight is not None:
                        await warmup_registry.attach_listener(msg_chat_id, self._send)
                        self._attached_warmups.add(msg_chat_id)
                        for past_ev in list(inflight.event_history):
                            await self._send(past_ev)
                        # Install progress arrives via the per-user broadcaster
                        # (+ connect-time replay), not a per-chat attach.
                        self.chat_id = msg_chat_id
                        self.agent_name = chat["agent"]
                        logger.info(
                            f"WS dashboard _handle_chat: attached to in-flight "
                            f"warmup chat={msg_chat_id}, replayed "
                            f"{len(inflight.event_history)} events"
                        )
                        return

                    self.chat_id = msg_chat_id
                    self.agent_name = chat["agent"]
                    chat_exec_path = chat.get("execution_path", "")
                    recover_role = _effective_agent_role(self.user_sub, self.agent_name, fallback_user=self.user)
                    # Pinned target for resume affinity — the recovered layer
                    # drives liveness/resume checks for this chat's session
                    # (see _do_warmup).
                    self.layer = get_execution_layer(
                        self.agent_name, execution_path=chat_exec_path,
                        user_sub=self.user_sub, role=recover_role,
                        execution_target=chat.get("execution_target") or "",
                    )
                    # If a pump is already running for this chat (orphaned task,
                    # meeting, or a turn from a now-dead WS), attach to it
                    # explicitly: re-acquire the concurrency slot, set session_id,
                    # send warmup_ready. The pump's existing live_state replay on
                    # next event keeps the UI in sync. Falls through to normal
                    # auto-warmup if no pump is active.
                    pump = _active_pumps.get(self.chat_id)
                    if pump and not pump.is_done:
                        from core.concurrency import acquire_chat_slot
                        # Pinned target so a REMOTE session's recovery stays off the local ceiling.
                        if await acquire_chat_slot(pump.session_id, target=chat.get("execution_target") or "local", execution_path=chat.get("execution_path")):
                            self.session_id = pump.session_id
                            await self._send({
                                "type": "warmup_ready",
                                "session_id": self.session_id,
                                "chat_id": self.chat_id,
                                "mode": get_session_mode(self.session_id) or chat.get("permission_mode", "default"),
                                "model": chat.get("model", ""),
                                "execution_path": chat_exec_path or "",
                            })
                            logger.info(
                                f"WS dashboard _handle_chat: recovered state from msg.chat_id, "
                                f"attached to active pump session={self.session_id}, chat={self.chat_id}, "
                                f"agent={self.agent_name}"
                            )
                        else:
                            await self._send_error("Platform busy — too many active sessions.")
                            return
                    else:
                        logger.info(
                            f"WS dashboard _handle_chat: recovered state from msg.chat_id "
                            f"(no warmup/resume seen on this WS), chat={self.chat_id}, agent={self.agent_name}"
                        )

        # ── Usage limit check ──
        # A dashboard chat always runs on the SENDER's subscription — Shared-only
        # agents included (only the mount + chat history are agent-scope there;
        # credentials follow the interacting user) — so gate on the sender's
        # platform-auth budget. The AGENT budget gates the platform-paid sources
        # (tasks / triggers / meetings / phone) at their own launch sites.
        try:
            from services.billing import usage_service
            limit_status = await asyncio.to_thread(
                usage_service.check_user_limit, self.user_sub, self.user_role
            )
            if not limit_status["allowed"]:
                await self._send({"type": "limit_reached", **limit_status["periods"]})
                return
            if limit_status["warning"]:
                await self._send({"type": "limit_warning", **limit_status["periods"]})
        except Exception:
            pass  # Don't block messages if limit check fails

        # Auto-warmup if we have agent but no session. background=False: this
        # turn needs the session synchronously (we read session_id just below),
        # so wait for the spawn rather than backgrounding it.
        if not self.session_id and self.agent_name and self.chat_id:
            await self._handle_warmup({
                "agent": self.agent_name,
                "chat_id": self.chat_id,
                "permission_mode": "default",
            }, background=False)

        # Auto-warmup if session died (idle reap, process crash, etc.)
        if self.session_id and self.agent_name and self.chat_id:
            alive = self.layer and await self.layer.is_session_alive(self.session_id)
            if not alive:
                logger.info(
                    f"WS dashboard _handle_chat: session {self.session_id} "
                    f"dead/reaped, auto-warming for chat={self.chat_id}"
                )
                if self.layer:
                    await self.layer.prepare_resume(self.session_id)
                # Release old slot before clearing (prepare_resume may have
                # removed session from registry, making reaper unable to clean up)
                from core.concurrency import release_chat_slot
                release_chat_slot(self.session_id)
                self.session_id = None
                await self._handle_warmup({
                    "agent": self.agent_name,
                    "chat_id": self.chat_id,
                    "permission_mode": "default",
                }, background=False)
            elif _vis.is_shared_only(self.agent_name):
                # Sender-pays (Shared-only): the live session is bound to the
                # subscription — and identity (JWT, OTO_USER_SUB, role) — of
                # whoever warmed it. When the conversation changes hands
                # BETWEEN turns, recycle the session so this turn binds the
                # current sender's subscription instead of silently spending
                # the previous sender's account. A message that lands while a
                # turn is streaming keeps the running turn's session (steer /
                # queue semantics unchanged — that turn stays on its starter).
                from services.engines import subscription_pool
                pump = _active_pumps.get(self.chat_id)
                turn_in_flight = bool(pump and not pump.is_done)
                payer = subscription_pool.get_session_payer_sub(self.session_id)
                if not turn_in_flight and payer and payer != self.user_sub:
                    logger.info(
                        f"WS dashboard _handle_chat: shared-only chat={self.chat_id} "
                        f"changed sender — recycling session {self.session_id} to "
                        f"bind the new sender's subscription"
                    )
                    try:
                        # close_session releases the slot + subscription; the
                        # JSONL stays, so the re-warm resumes the conversation.
                        await self.layer.close_session(self.session_id)
                    except Exception:
                        logger.warning(
                            f"sender-change recycle: close_session failed for "
                            f"{self.session_id}", exc_info=True,
                        )
                    self.session_id = None
                    await self._handle_warmup({
                        "agent": self.agent_name,
                        "chat_id": self.chat_id,
                        "permission_mode": "default",
                    }, background=False)

        if not self.session_id:
            await self._send_error("No session — send warmup first")
            return

        # Resolve the chat scope once. Shared-only agent dashboard chats run as
        # agent-scoped (one shared history) — sandbox mounts only `/workspace/`,
        # no `/users/{u}/`. All other agents' chats are user-scoped — sandbox
        # mounts `/users/{u}/` (and `/workspace/` for managers). Mirrors the
        # `pump_scope` derivation in `_start_new_stream` above.
        if not self.agent_name or not agent_store.agent_exists(self.agent_name):
            await self._send_error("Unknown agent")
            return
        is_agent_scoped = _vis.is_shared_only(self.agent_name)
        agent_dir = config.get_agent_dir(self.agent_name)
        username = self.user.get("username") or ""
        if not is_agent_scoped and not username:
            # User-scoped chat without a username slug indicates a user
            # provisioning gap (every account gets a slug on first login).
            await self._send_error("User has no username configured")
            return

        # Layer dispatch differs for chat-attached photos: CLI/Codex have a
        # built-in Read tool that opens the saved file from disk and submits
        # it to the API as a vision content block. Direct LLM has no built-in
        # Read tool — we attach the image directly to the user message as a
        # provider-native vision content block (Anthropic `image`/source.base64,
        # OpenAI `image_url`). See proxy/core/layers/providers/base.py
        # `format_image_content_block` and proxy/core/layers/direct/session.py
        # `run_direct_stream(images=...)`.
        # Use `layer.capabilities.name` rather than a local exec_path string
        # because `effective_exec_path` is scoped to the warmup/resume
        # handlers (not _handle_chat). Direct LLM is always local (never goes
        # through RemoteExecutionLayer per docs), so the layer's own name is
        # the right discriminator.
        is_direct_llm = bool(self.layer and self.layer.capabilities.name == "direct-llm")

        cli_text, attached_images, image_meta, valid_files = await self._process_attachments(
            text, images, files,
            agent=self.agent_name, agent_dir=agent_dir,
            is_agent_scoped=is_agent_scoped, username=username,
            is_direct_llm=is_direct_llm,
        )

        # Shared-only: detect a change of hands BEFORE persisting this message
        # (afterwards the sender IS the last author). Drives the speaker-
        # transition note injected below.
        prev_author = ""
        if is_agent_scoped and self.chat_id and not server_kick:
            prev_author = task_store.get_last_user_message_author(self.chat_id)

        # Save original user text to DB (without image/file paths injection)
        event_meta = {}
        if image_meta:
            event_meta["images"] = image_meta
        if valid_files:
            event_meta["files"] = valid_files
        event_data = json.dumps(event_meta) if event_meta else ""
        if not server_kick and not artifact_framed:
            task_store.add_chat_message(self.chat_id, "user", text, event_data=event_data, author_sub=self.user_sub)

        # Stable deterministic title at first-message time — set once when the
        # chat has no title yet. No LLM, no post-turn rename churn.
        if self.chat_id:
            chat_rec = task_store.get_chat(self.chat_id)
            if chat_rec and not chat_rec.get("title") and not artifact_framed:
                _title = self._deterministic_title(text)
                task_store.update_chat(self.chat_id, title=_title)
                await self._send({"type": "title_updated", "chat_id": self.chat_id, "title": _title})

            # Inject cancelled turn context if previous turn was aborted AND
            # the abort was a hard kill (killpg / stream cancel) — those paths
            # lose the partial turn engine-side, so we read it from our DB and
            # prepend it. A GRACEFUL abort (last_abort_graceful) kept the
            # partial turn in the engine's own history — injecting would
            # duplicate it. Skipped while a history seed is pending — the
            # digest injected at _start_new_stream already contains the
            # aborted turn.
            if chat_rec and chat_rec.get("last_turn_aborted") \
                    and not chat_rec.get("pending_history_seed"):
                graceful_abort = bool(chat_rec.get("last_abort_graceful"))
                task_store.update_chat(self.chat_id, last_turn_aborted=False,
                                       last_abort_graceful=False)
                if not graceful_abort:
                    cancelled = self._build_cancelled_context(self.chat_id)
                    if cancelled:
                        cli_text = cancelled + "\n\n" + cli_text
                        logger.info(f"WS dashboard: injected cancelled turn context ({len(cancelled)} chars) for chat={self.chat_id}")

        # Speaker-transition note (Shared-only): the resumed transcript may
        # carry other teammates' turns, and the warmup's identity line names
        # only the current sender — tell the agent who THIS message is from so
        # it doesn't attribute the earlier conversation to them. Prompt-side
        # only: the stored message row stays the raw text (author_sub carries
        # the attribution).
        if prev_author and prev_author != self.user_sub:
            _display = self.user.get("name") or self.user.get("username") or "another teammate"
            cli_text = (
                f"[Shared chat: this message is from {_display}; earlier user "
                f"messages may be from other teammates.]\n\n" + cli_text
            )

        # Create pump and enter streaming loop. The user turn targets the viewed
        # chat (the connection attributes) — the common, behaviour-preserving path.
        pump = await self._start_new_stream(
            cli_text,
            target_session_id=self.session_id,
            target_chat_id=self.chat_id,
            target_layer=self.layer,
            images=attached_images or None,
        )
        if pump:
            await self._enter_pump_loop()
            # Ephemeral "turn done" is now fired from pump cleanup (covers WS death case)

            # Process any messages queued during streaming (e.g. user clicked
            # "implement" before CLI finished). Send as a new turn.
            while self.message_queue and self.session_id:
                combined = "\n\n".join(self.message_queue)
                self.message_queue.clear()
                await self._send({"type": "queue_sent", "text": combined})
                task_store.add_chat_message(self.chat_id, "user", combined, author_sub=self.user_sub)
                pump = await self._start_new_stream(
                    combined,
                    target_session_id=self.session_id,
                    target_chat_id=self.chat_id,
                    target_layer=self.layer,
                )
                if pump:
                    await self._enter_pump_loop()
                else:
                    await self._send({"type": "done", "chat_id": self.chat_id or ""})
                    break
            # Leftover backchannel interactions (queued via the between-turns
            # dispatcher during a transient streaming state, so never adopted
            # by a producer) — deliver now rather than waiting for a future
            # user send. The rows persist here; the frames render the chips.
            while self.artifact_queue and self.session_id:
                from ws import artifact_interactions as _ai
                batch = list(self.artifact_queue)
                self.artifact_queue.clear()
                for it in batch:
                    task_store.add_chat_message(
                        self.chat_id, "event", "",
                        event_type=_ai.event_type(it),
                        event_data=_ai.event_row_json(it),
                    )
                    await self._send(_ai.ws_frame(it, self.chat_id or ""))
                pump = await self._start_new_stream(
                    _ai.frame_text(batch),
                    target_session_id=self.session_id,
                    target_chat_id=self.chat_id,
                    target_layer=self.layer,
                )
                if pump:
                    await self._enter_pump_loop()
                else:
                    await self._send({"type": "done", "chat_id": self.chat_id or ""})
                    break
        else:
            # Pump creation failed (dead session, error) — reset frontend streaming state
            await self._send({"type": "done", "chat_id": self.chat_id or ""})

    async def _handle_artifact_interaction(self, msg: dict):
        """Between-turns entry for a display_ui backchannel send (the
        in-stream twin lives in `_stream_via_pump`). Validates provenance +
        caps (ws/artifact_interactions.py — the browser-side consent chip is
        a UX guard, THIS is the boundary), then delivers AskUserQuestion-
        style: its own framed turn when idle, queued to the boundary while a
        turn streams. Acks `{type:"artifact_ack", token, status[, reason]}`.
        """
        from ws import artifact_interactions as _ai
        token = str(msg.get("token") or "")

        async def ack(status: str, reason: str = ""):
            frame: dict = {"type": "artifact_ack", "token": token, "status": status}
            if reason:
                frame["reason"] = reason
            await self._send(frame)

        chat_id = str(msg.get("chat_id") or "")
        if not chat_id or chat_id != (self.chat_id or ""):
            return await ack("denied", "not the viewed chat")
        interaction, err = _ai.validate_interaction(
            chat_id, token, str(msg.get("title") or ""), msg.get("payload"),
        )
        if interaction is None:
            return await ack("denied", err)
        # Meeting turn flow is managed — no page-event injection mid-meeting.
        if task_store.get_active_meeting_for_chat(chat_id):
            return await ack("unavailable", "meeting in progress")
        if await self._deny_task_continue(chat_id):
            return await ack("denied", "task chat not continuable")
        if not _ai.check_rate(chat_id, token):
            return await ack("denied", "rate limited")

        if self.streaming:
            # A turn is live but this connection isn't in its pump loop (the
            # in-loop case routes to the twin). Queue for the boundary.
            pump = _active_pumps.get(chat_id)
            if pump is not None:
                queued = pump.queue_artifact(interaction)
            elif len(self.artifact_queue) < _ai.QUEUE_CAP:
                self.artifact_queue.append(interaction)
                queued = True
            else:
                queued = False
            return await ack("queued" if queued else "denied",
                             "" if queued else "queue full")

        # Idle: persist the distinct row, then run the framed turn through the
        # normal chat path (auto-warmup, limits, cancelled-context — with the
        # _artifact_framed downgrades). Ack BEFORE the turn so the artifact's
        # button state settles while the agent works.
        task_store.add_chat_message(
            chat_id, "event", "",
            event_type="artifact_interaction",
            event_data=_ai.event_row_json(interaction),
        )
        await ack("sent")
        await self._handle_chat({
            "text": _ai.frame_text([interaction]),
            "chat_id": chat_id,
            "_artifact_framed": True,
        })

    async def _handle_app_action(self, msg: dict):
        """Between-turns entry for a pinned mini-app send_prompt action (the
        in-stream twin lives in `_stream_via_pump`; fire_task actions execute
        via REST in api/apps and never reach the WS). Same delivery contract
        as `_handle_artifact_interaction` — the approved prompt TEMPLATE is
        the one authority upgrade; everything else keeps the backchannel
        downgrades. Acks `{type:"app_action_ack", app_id, action_id, status
        [, reason]}`."""
        from ws import artifact_interactions as _ai
        app_id = str(msg.get("app_id") or "")
        action_id = str(msg.get("action_id") or "")

        async def ack(status: str, reason: str = ""):
            frame: dict = {"type": "app_action_ack", "app_id": app_id,
                           "action_id": action_id, "status": status}
            if reason:
                frame["reason"] = reason
            await self._send(frame)

        chat_id = str(msg.get("chat_id") or "")
        if not chat_id or chat_id != (self.chat_id or ""):
            return await ack("denied", "not the viewed chat")
        interaction, err = _ai.validate_app_action(
            chat_id, self.agent_name or "", self.user_sub or "",
            app_id, action_id, msg.get("args"),
        )
        if interaction is None:
            return await ack("denied", err)
        if task_store.get_active_meeting_for_chat(chat_id):
            return await ack("unavailable", "meeting in progress")
        if await self._deny_task_continue(chat_id):
            return await ack("denied", "task chat not continuable")
        if not _ai.check_rate(chat_id, f"app:{app_id}"):
            return await ack("denied", "rate limited")

        # A chat whose FIRST content is an app action (front-page button →
        # fresh chat) never gets the send-time title (the framed turn skips
        # it by design) — name it from the action itself, matching the chip:
        # "Infra Dashboard — Refresh data". The LLM title upgrade may still
        # improve it after the first completed turn.
        chat_rec = task_store.get_chat(chat_id)
        if chat_rec and not chat_rec.get("title"):
            _title = self._deterministic_title(
                f"{interaction['title'] or interaction['slug']} — "
                f"{interaction['label'] or interaction['action_id']}"
            )
            task_store.update_chat(chat_id, title=_title)
            await self._send({"type": "title_updated", "chat_id": chat_id,
                              "title": _title})

        if self.streaming:
            pump = _active_pumps.get(chat_id)
            if pump is not None:
                queued = pump.queue_artifact(interaction)
            elif len(self.artifact_queue) < _ai.QUEUE_CAP:
                self.artifact_queue.append(interaction)
                queued = True
            else:
                queued = False
            return await ack("queued" if queued else "denied",
                             "" if queued else "queue full")

        task_store.add_chat_message(
            chat_id, "event", "",
            event_type="app_action",
            event_data=_ai.event_row_json(interaction),
        )
        await ack("sent")
        await self._handle_chat({
            "text": _ai.frame_text([interaction]),
            "chat_id": chat_id,
            "_artifact_framed": True,
        })

    async def _handle_chat_read(self, msg: dict):
        """Mark a chat read for this viewer (sent by the client when the chat is
        open + focused). Upserts the per-(chat, owner-identity) marker driving
        the sidebar unread dot — shared-only chats key on the synthetic
        ``agent::`` owner, so ANY user's open clears it for everyone — then fans
        the clear out live so other tabs/users drop the dot without a refetch.
        Best-effort: bad ids / no access are silent no-ops."""
        cid = msg.get("chat_id") or ""
        if not cid:
            return
        chat = task_store.get_chat(cid)
        if not chat:
            return
        owner = chat.get("user_sub", "")
        chat_agent = chat.get("agent", "")
        if owner != self.user_sub and self.user_role != "admin":
            # Mirror the resume gate: assigned users may read agent-scoped
            # chats of shared-only agents (+ phone conversations).
            is_agent_scoped = (
                _vis.is_shared_only(chat_agent)
                or chat.get("source_type") == "phone"
                or _vis.is_shared_chat_owner(owner)
            )
            if not (chat_agent in self.user_agents and is_agent_scoped):
                return
        identity = _vis.chat_history_owner(chat_agent, self.user_sub)
        await asyncio.to_thread(task_store.mark_chat_read, cid, identity)
        notification_manager.broadcast_chat_read(owner, cid, agent=chat_agent)

    async def _handle_resume_chat(self, msg: dict):
        self.promised_pump_chat = None  # every resume resets the previous promise
        self._detach_pty_viewer()  # leaving any interactive chat we were viewing

        cid = msg.get("chat_id", "")
        if not cid:
            await self._send_error("chat_id required")
            return

        chat = task_store.get_chat(cid)
        if not chat:
            await self._send_error("Chat not found")
            return
        if chat["user_sub"] != self.user_sub and self.user_role != "admin":
            # Allow assigned users to view a Shared-only agent's agent-scoped
            # chats (phone conversations, tasks, meetings, shared history).
            chat_agent = chat.get("agent", "")
            is_assigned = chat_agent in self.user_agents
            is_agent_scoped = (
                _vis.is_shared_only(chat_agent)
                or chat.get("source_type") == "phone"
                or _vis.is_shared_chat_owner(chat["user_sub"])
            )
            if not (is_assigned and is_agent_scoped):
                await self._send_error("Access denied")
                return

        # In-flight warmup re-attach: if a warmup is still running (fresh
        # satellite first chat, ~90s MCP install) and our WS reconnected,
        # attach as a listener and replay event history so the UI catches
        # up. The original _handle_warmup coroutine keeps running and the
        # eventual warmup_ready reaches us via the registry — do NOT touch
        # chat_history or kick a new warmup here.
        inflight = warmup_registry.get(cid)
        if inflight is not None:
            await warmup_registry.attach_listener(cid, self._send)
            self._attached_warmups.add(cid)
            self.chat_id = cid
            self.agent_name = chat["agent"]
            # (DB-first read): the first prompt was persisted at
            # send-time (_persist_first_prompt), so a switch-away/back or a mid-spawn
            # refresh must reload it from the DB — without this the bubble is blank
            # until the turn streams (the "my prompt disappeared while it was getting
            # ready" bug). Send chat_history FIRST so the frontend clears its
            # switch-away discard guard (onChatHistory) before the replayed warmup_*
            # events re-render the "Getting ready" state on top of the prompt.
            inflight_msgs, inflight_has_more = task_store.get_chat_messages_page(cid, _CHAT_PAGE)
            await self._send({
                "type": "chat_history",
                "chat_id": cid,
                "agent": chat["agent"],  # agent of record — URL normalization
                "messages": inflight_msgs,
                "has_more": inflight_has_more,
                "restore": _build_chat_restore(cid),
                "plans": [],
                "total_cost": chat.get("total_cost") or 0,
                "context_used": chat.get("context_used") or 0,
                "context_max": chat.get("context_max") or 0,
                "cache_read": chat.get("cache_read") or 0,
                "cache_write": chat.get("cache_write") or 0,
                "output_tokens": chat.get("output_tokens") or 0,
                "execution_path": resolve_execution_path(self.agent_name, chat.get("execution_path", "")),
                "execution_mode": chat.get("execution_mode", ""),
                "model": chat.get("model", ""),
                "mode": chat.get("permission_mode", "default"),
            })
            for past_ev in list(inflight.event_history):
                await self._send(past_ev)
            # Install events are delivered out-of-band via the per-user
            # broadcaster + replayed on connect (see the WS setup), so there
            # is no per-chat install attach to do here.
            logger.info(
                f"WS dashboard resume_chat: attached to in-flight warmup "
                f"chat={cid}, sent {len(inflight_msgs)} msgs + replayed "
                f"{len(inflight.event_history)} events"
            )
            return

        self.chat_id = cid
        self.agent_name = chat["agent"]
        # Resolve execution layer from the chat's stored path AND pinned
        # target, falling back to agent defaults. The pin matters: the
        # liveness/resume checks this layer serves must ask the machine the
        # chat's session actually lives on, not wherever the agent is
        # currently targeted (resume affinity — see _do_warmup).
        chat_exec_path = chat.get("execution_path", "")
        resume_role = _effective_agent_role(self.user_sub, self.agent_name, fallback_user=self.user)
        self.layer = get_execution_layer(
            self.agent_name, execution_path=chat_exec_path, user_sub=self.user_sub,
            role=resume_role, execution_target=chat.get("execution_target") or "",
        )
        effective_exec_path = resolve_execution_path(self.agent_name, chat_exec_path)
        chat_model = chat.get("model", "")
        total_cost = chat.get("total_cost") or 0
        context_used = chat.get("context_used") or 0
        context_max = chat.get("context_max") or 0
        cache_read = chat.get("cache_read") or 0
        cache_write = chat.get("cache_write") or 0
        output_tokens = chat.get("output_tokens") or 0
        # task-{runId} chats aggregate sibling-turn histories below; a scroll-back
        # cursor over a mixed-id set is incoherent, so they load FULL (no paging).
        # Every other chat loads the newest page; older turns lazy-load on scroll-up.
        is_task_chat = cid.startswith("task-")
        if is_task_chat:
            messages = task_store.get_chat_messages(cid)
            has_more = False
        else:
            messages, has_more = task_store.get_chat_messages_page(cid, _CHAT_PAGE)

        # Multi-turn task runs: include messages from all turns in the session.
        # Each turn has its own chat_id (task-{runId}) but shares session_id.
        # For turns with an active pump, truncate THAT turn's messages only
        # (live_state from the pump will provide the streaming content).
        active_task_pump = None
        if cid.startswith("task-"):
            run_id = cid.removeprefix("task-")
            run = task_store.get_run(run_id)
            if run and run.get("session_id"):
                related = task_store.list_runs(
                    limit=50, session_id=run["session_id"],
                )
                related.sort(key=lambda r: r.get("started_at") or "")
                for r in related:
                    if r["id"] == run_id:
                        continue
                    if r.get("chat_id") and r["chat_id"] != cid:
                        extra_msgs = task_store.get_chat_messages(r["chat_id"])
                        # If this turn has an active pump, truncate only ITS messages
                        rp = _active_pumps.get(r["chat_id"])
                        if rp and not rp.is_done:
                            active_task_pump = rp
                            # id-based cutoff: withhold the in-flight tail (live_state
                            # provides it). A row-count slice would over-keep once the
                            # window is paged, re-rendering live rows as ghost bubbles.
                            # USER rows are exempt: a mid-turn STEERED message persists
                            # above the cutoff and the pump replay never re-sends user
                            # rows — filtering it made the message vanish on revisit.
                            extra_msgs = [
                                m for m in extra_msgs
                                if int(m["id"]) <= rp._db_msg_cutoff_id
                                or m.get("role") == "user"
                            ]
                        if extra_msgs:
                            messages.extend(extra_msgs)

        plans = task_store.get_chat_plans(cid)
        # Restore plan filename from DB so subsequent pumps reuse it
        if plans and not self.chat_plan_filename:
            self.chat_plan_filename = plans[-1]["filename"]

        old_session_id = chat.get("session_id")
        perm_mode = chat.get("permission_mode", "default")

        # Reap a WEDGED primary pump before truncating/attaching (stuck-chat
        # recovery). A remote turn whose satellite event stream was severed (a
        # reconnect orphaned the session queue, so post-reconnect events are
        # dropped) leaves its producer parked on q.get() with no activity — the
        # pump never finishes, so a plain resume would re-attach to a zombie
        # that re-shows "streaming" forever (the reported stuck chat). Detect it
        # via the producer being done OR the remote session being idle past the
        # ceiling, and reap instead of re-attach. pump.abort() cancels the
        # wedged producer (its _produce finally emits PRODUCER_DONE → the pump
        # exits + del _active_pumps); we await that teardown. A healthy
        # streaming turn advances last_activity on every event so it never trips
        # the staleness check and is never reaped.
        _wedged = _active_pumps.get(cid)
        if _wedged and not _wedged.is_done and self.layer is not None:
            _severed = self.layer.remote_stream_severed(_wedged.session_id)
            _idle = self.layer.session_idle_seconds(_wedged.session_id)
            _stale = _idle is not None and _idle > STALE_TURN_SECS
            # Event silence past the soft ceiling is NOT death: a network
            # stall on the satellite box leaves the CLI alive and working
            # with zero stream events for many minutes (the Mode D
            # incident — reaped at idle=505s, answer orphaned). Silence only
            # ARMS a liveness probe; an alive process gets a long leash and
            # is reaped only past the CLI turn ceiling.
            _hard_stale = _idle is not None and _idle > config.CLAUDE_TIMEOUT
            _proc_dead = False
            if _stale and not _severed and not _hard_stale:
                _proc_dead = await self.layer.probe_session_process_dead(
                    _wedged.session_id,
                )
            # `_unusable` = the remote session can't carry new turns (queue
            # orphaned by a reconnect — detected immediately — dead process,
            # or silent past the hard ceiling). `producer.done()` is the
            # benign cleanup-race case (turn finished, session still fine →
            # reap the lingering pump, keep session).
            _unusable = _severed or (_stale and _proc_dead) or _hard_stale
            if _wedged.producer.done() or _unusable:
                if _severed:
                    _reason = "stream severed by a satellite reconnect"
                elif _hard_stale:
                    _reason = (
                        f"stalled: no stream events for {int(_idle)}s, "
                        f"hard ceiling exceeded"
                    )
                else:
                    _reason = (
                        f"stalled: no stream events for {int(_idle or 0)}s, "
                        f"process dead"
                    )
                logger.warning(
                    f"WS dashboard resume_chat: reaping wedged pump chat={cid} "
                    f"session={_wedged.session_id[:8]} (producer_done="
                    f"{_wedged.producer.done()}, severed={_severed}, "
                    f"idle={_idle}, proc_dead={_proc_dead})"
                )
                # A running task run riding this chat: stamp WHY the platform
                # stopped it (the runs page must distinguish this from a user
                # cancel) and cancel its scheduler task before the abort.
                if _unusable and cid.startswith("task-"):
                    from services.scheduler import scheduler as _sched
                    _rid = cid.removeprefix("task-")
                    _full = f"reaped by platform: {_reason}"
                    if not _sched.platform_cancel_run(_rid, _full):
                        _run = task_store.get_run(_rid)
                        if _run and _run.get("status") == "running":
                            task_store.update_run(
                                _rid, status="failed", error_message=_full,
                                completed_at=datetime.now(timezone.utc).isoformat(),
                            )
                _wedged.abort()
                if _wedged._task is not None:
                    # Wait WITHOUT cancelling — the pump's finally persists the
                    # partial turn; a 2s laggard is simply left to finish.
                    await asyncio.wait([_wedged._task], timeout=2.0)
                if _unusable:
                    # The remote session is unusable (orphaned/severed event
                    # queue), so force a fresh spawn on the next message.
                    # prepare_resume pops it (+ tears down its bg router); the
                    # next auto-warmup resumes the conversation via the codex
                    # thread_id / CLI --resume, so chat memory is preserved.
                    try:
                        await self.layer.prepare_resume(_wedged.session_id)
                    except Exception:
                        logger.exception(f"reap prepare_resume failed for chat={cid}")
                    from core.concurrency import release_chat_slot
                    release_chat_slot(_wedged.session_id)
            elif _stale:
                logger.info(
                    f"WS dashboard resume_chat: chat={cid} stalled but process "
                    f"alive (idle={_idle:.0f}s) — leaving the turn to recover"
                )

        # Check for active pump on primary chat or related task turn
        pump = _active_pumps.get(cid)
        # An ACTIVE external session (phone today; website/webhook later) owns
        # this chat's stream out-of-band. Never attach to its pump — that would
        # steal the stream and kill the live call. Serve the FULL persisted
        # transcript read-only instead (no truncation, no attach below).
        view_only_external = bool(
            pump and not pump.is_done
            and pump.source_type in _EXTERNAL_DRIVEN_SOURCES
        )
        if pump and not pump.is_done and not view_only_external:
            # Primary chat has active pump — withhold ITS in-flight tail by id
            # (the live stream re-sends it; a count slice breaks under paging).
            # USER rows are exempt: a mid-turn STEERED message (persisted the
            # moment the daemon accepts it, id ABOVE the cutoff) is NOT part of
            # the pump's replayed blocks, so filtering it made the message
            # vanish when the user switched away and back mid-turn. The pump
            # never re-sends user rows (queued-not-yet-delivered messages ride
            # queue_snapshot and aren't persisted yet), so no double-render.
            messages = [
                m for m in messages
                if int(m["id"]) <= pump._db_msg_cutoff_id
                or m.get("role") == "user"
            ]
        elif active_task_pump:
            # Related turn has active pump — primary messages are complete,
            # related turn's messages already truncated above
            pump = active_task_pump

        await self._send({"type": "chat_history",
                     "chat_id": cid,
                     # Agent of record, from the chat row — the frontend uses it
                     # to normalize a mismatched /chat/:name/:chatId URL (a
                     # deep-link/redirect can carry the wrong slug; the route
                     # param is otherwise trusted for the UI shell).
                     "agent": chat["agent"],
                     "messages": messages,
                     "has_more": has_more,
                     "restore": _build_chat_restore(cid),
                     "plans": [{"filename": p["filename"], "content": p["content"],
                                "status": p["status"]} for p in plans],
                     "total_cost": total_cost,
                     "context_used": context_used,
                     "context_max": context_max,
                     "cache_read": cache_read,
                     "cache_write": cache_write,
                     "output_tokens": output_tokens,
                     "execution_path": effective_exec_path,
                     "execution_mode": chat.get("execution_mode", ""),
                     "model": chat.get("model", ""),
                     # The chat's stored permission mode. The frontend applies
                     # it for task-run chats (the scheduler's 'auto' posture)
                     # so the permission chip reflects the RUN's real mode,
                     # not the viewer's sticky selection.
                     "mode": chat.get("permission_mode", "default")})

        if view_only_external:
            # Read-only view of a live external session: history is sent, but we
            # do NOT acquire the session slot, send warmup_ready, or promise a
            # pump attach — leaving the phone/webhook driver's stream untouched
            # so the call keeps playing and tears down normally.
            logger.info(
                f"WS dashboard: chat={cid} driven by an active "
                f"{pump.source_type} session — read-only view, not attaching"
            )
            return

        if pump and not pump.is_done:
            # Re-acquire concurrency slot (released on a prior WS disconnect),
            # using the chat's pinned target so a REMOTE session stays off the
            # local ceiling G.
            from core.concurrency import acquire_chat_slot
            adm = await acquire_chat_slot(pump.session_id, target=chat.get("execution_target") or "local", execution_path=chat.get("execution_path"))
            if not adm:
                await self._send_error(adm.user_message)
                return
            self.session_id = pump.session_id
            # Refresh session's user_tz from this user's last client_info —
            # handles the WS-reconnect case where session was originally
            # warmed up before any client_info had arrived.
            _user_default_tz = get_user_tz(self.user_sub)
            if _user_default_tz:
                set_session_user_tz(self.session_id, _user_default_tz)
            await self._send({
                "type": "warmup_ready",
                "session_id": self.session_id,
                "chat_id": self.chat_id,
                "mode": perm_mode,
                "model": chat_model,
                "execution_path": effective_exec_path,
                **self._target_mismatch_fields(self.chat_id),
            })
            # Resync any reload-persisted queuedMessages on the
            # client against the pump's actual queue. Backend is the source
            # of truth; without this a reload mid-streaming would show
            # stale entries (we might have missed a queue_sent during the
            # disconnect). list() snapshots safely without holding the
            # producer's reference.
            await self._send({
                "type": "queue_snapshot",
                "chat_id": self.chat_id,
                "messages": list(getattr(pump, "message_queue", []) or []),
            })
            # live_state is sent from _stream_via_pump AFTER attach() to avoid race
            self.promised_pump_chat = cid  # consumed by _enter_pump_loop
            logger.info(f"WS dashboard: active pump found for chat={self.chat_id}, will attach")
            return

        # Try to reconnect to existing session
        if old_session_id:
            # Live INTERACTIVE session (PTY) — re-attach the viewer. Checked
            # before the headless is_session_alive (PTY sessions aren't in the
            # layer registry).
            isess = interactive_session.get(old_session_id)
            if isess is not None and isess.alive:
                from core.concurrency import acquire_chat_slot
                # Live session's target → a REMOTE interactive re-attach is a no-op.
                await acquire_chat_slot(old_session_id, target=isess.target)
                self.session_id = old_session_id
                _user_default_tz = get_user_tz(self.user_sub)
                if _user_default_tz:
                    set_session_user_tz(self.session_id, _user_default_tz)
                await self._send({
                    "type": "warmup_ready",
                    "session_id": self.session_id,
                    "chat_id": self.chat_id,
                    "mode": get_session_mode(old_session_id) or perm_mode,
                    "model": chat_model,
                    "execution_path": effective_exec_path,
                    "interactive": True,
                    # Live turn state at attach — same reconciliation as the
                    # warmup re-attach path: the chat_history sent above makes
                    # the client reset this chat to 'ready' (expecting a pump
                    # live_state that interactive sessions never send), so
                    # without this field visiting a mid-turn chat killed its
                    # sidebar live state for good (broadcasts are
                    # transition-only; finishWarmup maps True → streaming).
                    "turn_open": isess.turn_open,
                    **self._target_mismatch_fields(self.chat_id),
                })
                await self._send({"type": "queue_snapshot", "chat_id": self.chat_id, "messages": []})
                # Client attaches the PTY viewer via pty_attach (see _dispatch).
                return
            if self.layer and await self.layer.is_session_alive(old_session_id):
                # Re-acquire concurrency slot (released on WS disconnect), using
                # the chat's pinned target so a REMOTE session stays off G.
                from core.concurrency import acquire_chat_slot
                adm = await acquire_chat_slot(old_session_id, target=chat.get("execution_target") or "local", execution_path=chat.get("execution_path"))
                if not adm:
                    await self._send_error(adm.user_message)
                    return
                self.session_id = old_session_id
                # Refresh session's user_tz from this user's last client_info.
                _user_default_tz = get_user_tz(self.user_sub)
                if _user_default_tz:
                    set_session_user_tz(self.session_id, _user_default_tz)
                live_mode = get_session_mode(old_session_id) or perm_mode
                await self._send({
                    "type": "warmup_ready",
                    "session_id": self.session_id,
                    "chat_id": self.chat_id,
                    "mode": live_mode,
                    "model": chat_model,
                    "execution_path": effective_exec_path,
                    "interactive": False,
                    **self._target_mismatch_fields(self.chat_id),
                })
                # No active pump → queue is empty by definition. Emit so
                # the client clears any reload-persisted stale entries.
                await self._send({"type": "queue_snapshot", "chat_id": self.chat_id, "messages": []})
                return

        # Session dead or doesn't exist — DON'T spawn a new process just for browsing.
        # Send warmup_ready with no session. When the user sends a message,
        # _handle_chat -> _start_new_stream will auto-resume the session on demand.
        #
        # Clear stale pending permissions — the hook scripts died with the CLI
        # process, so these can never be resolved. Permission prompts are rejected.
        # Plan reviews are kept pending — the user can still act on them after
        # session resume (auto-approve via implementing_plan flag).
        if old_session_id:
            stale_perm = _pending_permissions.pop(old_session_id, None)
            if stale_perm and self.chat_id:
                evt_type = stale_perm.get("event_type", "")
                if evt_type == "permission_prompt":
                    task_store.add_chat_message(
                        self.chat_id, "event", "",
                        event_type="permission_prompt",
                        event_data=json.dumps({
                            "type": "permission_prompt",
                            "request_id": stale_perm.get("request_id", ""),
                            "tool_name": stale_perm.get("tool_name", ""),
                            "tool_input": stale_perm.get("tool_input", {}),
                            "resolved": True, "approved": False,
                        }),
                    )
                    logger.info(f"WS dashboard: saved stale permission as rejected for chat={self.chat_id}")
                elif evt_type == "plan_review":
                    # Keep plan_review pending — user can still implement after session resume
                    logger.info(f"WS dashboard: keeping stale plan_review pending for chat={self.chat_id}")
        self.session_id = None
        await self._send({
            "type": "warmup_ready",
            "session_id": None,
            "chat_id": self.chat_id,
            "mode": perm_mode,
            "model": chat_model,
            "execution_path": effective_exec_path,
            "execution_mode": chat.get("execution_mode", ""),
            "needs_warmup": True,
            **self._target_mismatch_fields(self.chat_id),
        })
        # Lazy path — no pump, no session yet. Clear any persisted queue
        # on the client (rare but possible if user reloaded after a turn
        # finished but before sending again).
        await self._send({"type": "queue_snapshot", "chat_id": self.chat_id, "messages": []})
        logger.info(
            f"WS dashboard resume_chat (lazy): session dead/missing, "
            f"chat={self.chat_id}, agent={self.agent_name} — will warmup on first message"
        )

    async def _start_new_stream(self,
        prompt: str,
        *,
        target_session_id: str | None,
        target_chat_id: str,
        target_layer: ExecutionLayer | None,
        images: list[dict] | None = None,
    ) -> ChatStreamPump | None:
        """Create a producer + pump for a streaming turn on an EXPLICIT target.

        The turn executes against ``(target_session_id, target_chat_id,
        target_layer)`` — NOT the connection's *viewed* chat. The pump is keyed
        by ``target_chat_id`` in ``_active_pumps`` and persists to the DB whether
        or not this socket is attached, so a server-initiated turn (the
        server-kick, a bg-agent nudge, a delegate ``task_result``) lands in the
        right chat even when the user is viewing a different one. The single
        socket reader (``_enter_pump_loop``) attaches this socket only when the
        target IS the viewed chat.

        ``is_viewed`` (``target_chat_id == chat_id``) gates everything that is
        legitimately connection-scoped: a turn on the viewed chat warms a dead
        session via ``adopt=True`` (mutating the ``session_id``/``layer``
        attributes) and may adopt the connection's pending ``message_queue`` /
        ``implementing_plan`` / ``chat_plan_filename``. A server turn for a
        non-viewed chat warms via ``adopt=False`` (the warmed session is
        used locally for this turn, the viewed attributes are never touched) and
        adopts none of the connection-scoped queues.

        ``images`` (Direct LLM only): list of ``{"base64", "media_type"}`` for
        chat-attached photos, forwarded to ``send_message(images=...)`` for the
        initial turn only. CLI/Codex ignore the kwarg via ``**kwargs``.
        """

        sid = target_session_id
        if not sid:
            await self._send_error("No session — send warmup first")
            return None

        # The chat this socket is currently viewing. Connection-scoped state may
        # only be touched when the turn targets it.
        is_viewed = target_chat_id == self.chat_id

        # Check for dead process — handles abort and idle-timeout cases where the
        # session exists but the process has exited. Target agent / exec_path /
        # pinned target are resolved from the CHAT ROW (not connection attributes),
        # which also removes the old cross-handler ``effective_exec_path``
        # coupling. A dead VIEWED target is auto-resumed into the viewed attributes
        # (adopt=True); a dead NON-viewed target (a server turn whose originating
        # chat was reaped) is warmed via adopt=False and run headless on the
        # returned spawn — never clobbering the viewed chat's session.
        process_dead = bool(target_layer) and await target_layer.is_session_process_dead(sid)
        if target_layer and not process_dead:
            # Turn-start token guard: never dispatch a turn onto an OAuth token
            # that expires within the turn-safety margin — a long turn would
            # die mid-run with the CLI's "Please run /login" (which a platform
            # session cannot answer). Refresh + fan-out IN PLACE: the live CLI
            # picks the rewritten credential file up (Claude: mtime-watch /
            # 401-recovery; Codex: guarded reload), so NO restart is needed.
            # The freshness worker covers idle sessions between turns; this
            # chokepoint covers the moment that actually hurts, for every turn
            # source (user send, TaskRunView, queue drain, server nudge). The
            # expiry snapshot tracks the token in the session's credential
            # FILE (spawn + fan-out; see subscription_pool.bind_session), so a
            # fail-soft spawn that inherited a short-runway stored token is
            # caught here too. Fail-soft: a failed refresh dispatches anyway —
            # the CLI's 401-recovery re-reads the file once a later attempt
            # repairs it, and blocking the turn would help nothing.
            from services.engines.subscription_pool import (
                TURN_MIN_TOKEN_RUNWAY_MS, ensure_fresh_and_fan_out,
                get_session_subscription, session_token_expiry_ms,
            )
            _exp_ms = session_token_expiry_ms(sid)
            if _exp_ms and time.time() * 1000 > _exp_ms - TURN_MIN_TOKEN_RUNWAY_MS:
                _sub_id = get_session_subscription(sid)
                if _sub_id:
                    logger.info(
                        f"WS dashboard: session {sid} token runway low — "
                        f"refresh + fan-out before dispatching the turn"
                    )
                    _fresh = await asyncio.to_thread(
                        ensure_fresh_and_fan_out, _sub_id, TURN_MIN_TOKEN_RUNWAY_MS,
                    )
                    if not _fresh:
                        logger.warning(
                            f"WS dashboard: token refresh failed for session {sid} — "
                            f"dispatching on the aging token (401-recovery backstop)"
                        )
        if process_dead:
            try:
                if is_viewed:
                    logger.info(f"WS dashboard: session {sid} process dead, auto-resuming")
                    await self._resume_dead_session_for_chat(
                        sid, target_chat_id, target_layer, adopt=True,
                    )
                    # _create_or_resume_session adopted the (re)created id into the
                    # self.session_id — adopt it as this turn's session.
                    sid = self.session_id
                else:
                    logger.info(
                        f"WS dashboard: server-turn target session {sid} dead and chat "
                        f"{target_chat_id} not viewed — warming headless"
                    )
                    res = await self._resume_dead_session_for_chat(
                        sid, target_chat_id, target_layer, adopt=False,
                    )
                    # Use the freshly-warmed session + layer for this turn ONLY;
                    # the viewed attributes were never touched (adopt=False).
                    sid = res.session_id
                    target_layer = res.layer
            except Exception as e:
                logger.error(f"WS dashboard: auto-resume failed: {e}", exc_info=True)
                if is_viewed:
                    await self._send_error(f"Session died and auto-resume failed: {e}")
                return None

        if not target_layer or not await target_layer.is_session_alive(sid):
            await self._send_error("Session not found — send warmup")
            return None

        # A chat flagged pending_history_seed lost its
        # on-disk session (machine deleted / files aged out) — prepend the
        # DB-history digest to this fresh session's first turn and surface
        # the one-time persisted notice. Single chokepoint: every turn (user
        # send, TaskRunView send, queue drain, server nudge) passes through
        # here, AFTER the alive checks so the flag is never burned on a turn
        # that fails to start. Direct-LLM rebuilds full history from the DB
        # on its own — never seeded.
        if target_chat_id and target_layer.capabilities.name != "direct-llm":
            prompt, reseed_notice = consume_pending_seed(target_chat_id, prompt)
            if reseed_notice and is_viewed:
                await self._send({
                    "type": "system", "subtype": "session_reseeded",
                    "message": reseed_notice, "chat_id": target_chat_id,
                })

        # Durable delegate-wake replay: results whose delivery failed entirely
        # (dead parent, resume failed) were stored on the chat — inject them
        # ahead of this turn's prompt so the orchestrator finally processes
        # them. Same chokepoint rationale + atomic claim as the seed above.
        if target_chat_id:
            pending_wakes = task_store.claim_pending_delegate_wake(target_chat_id)
            if pending_wakes:
                prompt = "\n\n".join(pending_wakes + [prompt])
                logger.info(
                    f"WS dashboard: injected {len(pending_wakes)} pending delegate "
                    f"wake(s) into turn for chat={target_chat_id[:8]}"
                )

        perm_queue = get_permission_queue(sid)

        # Message queue: only a turn on the VIEWED chat drains the connection's
        # pending user messages (they were queued against that chat). A server
        # turn for another chat starts with an empty queue.
        if is_viewed:
            msg_queue: list[str] = list(self.message_queue)
            self.message_queue.clear()
            art_queue: list[dict] = list(self.artifact_queue)
            self.artifact_queue.clear()
        else:
            msg_queue = []
            art_queue = []
        # System prompt queue: delivered silently (no user bubble).
        # Shared with pump — external code can append during streaming.
        sys_queue: list[str] = []

        # Memory-capture nudge: after N user turns without a memory tool
        # call, one reminder line rides this turn's outgoing message. The
        # user message was already persisted above — the reminder never
        # renders in the chat UI or DB. Layer-agnostic (CLI/Codex/Direct).
        if is_viewed:
            try:
                from services.memory import memory_nudge
                _nudge_line = memory_nudge.maybe_nudge(sid)
                if _nudge_line:
                    prompt = (
                        f"{prompt}\n\n<system-reminder>{_nudge_line}"
                        "</system-reminder>"
                    )
            except Exception:
                pass

        event_queue: asyncio.Queue = asyncio.Queue()

        async def _produce():
            try:
                async with target_layer.session_lock(sid):
                    # Images are attached only to the initial turn — queued
                    # follow-up messages are plain text. CLI/Codex layers
                    # ignore the kwarg; Direct LLM uses it to build vision
                    # content blocks.
                    initial_kwargs = {"inject_time": True}
                    if images:
                        initial_kwargs["images"] = images
                    async for event in target_layer.send_message(sid, prompt, **initial_kwargs):
                        await event_queue.put(event)
                    # Process queued messages (user + system) after each turn
                    # Flush pending control requests (mode changes, etc.) between turns
                    # while we still hold the session lock.
                    if self.pending_control_requests:
                        for subtype, kwargs in self.pending_control_requests:
                            try:
                                await target_layer.send_control_request(sid, subtype, **kwargs)
                            except Exception as e:
                                logger.warning(f"Between-turn control_request {subtype} error: {e}")
                        self.pending_control_requests.clear()
                    while msg_queue or art_queue or sys_queue:
                        if msg_queue:
                            combined = "\n\n".join(msg_queue)
                            msg_queue.clear()
                            await event_queue.put(CommonEvent(type=QUEUE_TURN, data={"text": combined}))
                            async for event in target_layer.send_message(sid, combined, inject_time=True):
                                await event_queue.put(event)
                        # Artifact interactions drain AFTER user words (lower
                        # authority) as their own framed turn per batch; the
                        # ARTIFACT_TURN handler persists the distinct rows.
                        if art_queue:
                            from ws import artifact_interactions as _ai
                            batch = list(art_queue)
                            art_queue.clear()
                            framed = _ai.frame_text(batch)
                            await event_queue.put(CommonEvent(
                                type=ARTIFACT_TURN,
                                data={"interactions": batch, "text": framed},
                            ))
                            async for event in target_layer.send_message(sid, framed, inject_time=True):
                                await event_queue.put(event)
                        # System prompts: delivered silently (no queue_turn event)
                        while sys_queue:
                            sys_prompt = sys_queue.pop(0)
                            async for event in target_layer.send_message(sid, sys_prompt):
                                await event_queue.put(event)
            except Exception as e:
                await event_queue.put(CommonEvent(type=ERROR, data={"message": str(e)}))
            finally:
                await event_queue.put(CommonEvent(type=PRODUCER_DONE, data={}))

        producer = asyncio.create_task(_produce())

        # Fallback usage scope — on a Shared-only agent the chat row's owner is
        # synthetic, so "agent" is the bucket when no payer is known. When a USER
        # subscription served the session, _record_usage re-attributes the row to
        # that payer (user scope). Use the target chat's agent (resolve from the
        # row only for a non-viewed server turn; the viewed turn's agent is the
        # connection's agent_name).
        scope_agent = self.agent_name if is_viewed else (
            (task_store.get_chat(target_chat_id) or {}).get("agent") or self.agent_name
        )
        pump_scope = "agent" if _vis.is_shared_only(scope_agent) else "user"

        pump = ChatStreamPump(
            chat_id=target_chat_id,
            session_id=sid,
            producer=producer,
            event_queue=event_queue,
            perm_queue=perm_queue,
            implementing_plan=self.implementing_plan if is_viewed else "",
            scope=pump_scope,
        )
        pump.message_queue = msg_queue  # share the same list with producer
        pump.system_queue = sys_queue   # share system prompt queue with producer
        pump.artifact_queue = art_queue  # share artifact-interaction queue too
        if is_viewed:
            pump._plan_filename = self.chat_plan_filename  # inherit from previous pump
        _active_pumps[target_chat_id] = pump
        pump.start()
        # Watch this turn for background work it leaves behind — the ONE arming
        # site that covers every turn source (user send, queue drain, server
        # kick/nudge), viewed or detached. _arm_bg_monitor_after awaits the pump
        # task, which completes only after the producer's finally ran — i.e.
        # AFTER Codex's turn-end hand-off registered its still-running bg
        # sub-agents (the turn-end hook in _enter_pump_loop could race that
        # registration and see has_pending=False, and a pump whose viewer
        # switched chats mid-turn had no arming site at all → its badges never
        # cleared and the review nudge never fired). Idempotent with the other
        # _ensure_bg_monitor callers via the monitors' per-session guards.
        asyncio.create_task(
            self._arm_bg_monitor_after(pump, sid, target_chat_id, target_layer)
        )
        if is_viewed:
            self.implementing_plan = ""  # pump owns it now
        return pump

    async def _stream_via_pump(self, pump: ChatStreamPump) -> dict:
        """Attach to a pump and stream events to the WebSocket.

        Handles client messages (permissions, queue, abort, chat switch).
        Returns {"detached": bool, "resume_msg": dict|None}.
        """

        result = {"detached": False, "resume_msg": None}
        self.streaming = True
        ws_queue = pump.attach()

        # Viewed-chat tag for every frame this loop emits. Deliberately the
        # connection's current chat, NOT pump.chat_id: multi-turn task chats
        # stream a LATER run's pump into the first run's view (_find_task_pump),
        # and the frontend drops stream frames tagged for a chat it isn't
        # showing (background chats must not render into the wrong view).
        frame_chat_id = self.chat_id or pump.chat_id

        # Tell the client the viewed chat is streaming, DIRECTLY on this
        # socket. The pump broadcasts the same chat_status via the per-user
        # notify queue, but that queue is drained only BETWEEN the viewed
        # chat's turns — so for the turn THIS loop is about to stream, the
        # broadcast can never land mid-turn. Without the direct send, the
        # dead-session resend path (abort → send → auto-rewarm → turn) left
        # the slice on warmup_ready's 'ready' for the whole turn: no stop
        # button, no timer, until a refresh rebuilt from live_state. Ordered
        # after warmup_started/warmup_ready (same socket), duplicate-safe
        # (setStreaming is idempotent).
        await self._send({"type": "chat_status", "chat_id": frame_chat_id,
                     "status": "streaming"})

        # Send live_state AFTER attach — no gap between snapshot and subscriber.
        # Send if there's any content worth restoring.
        live = _chat_streaming_state.get(pump.chat_id)
        if live and (live.get("streaming")
                     # ^ an active turn with NO output yet must still be
                     # announced — it restores the timer/stop/streaming state
                     # when the user returns to a chat whose turn hasn't
                     # produced content (long spawn / model still thinking).
                     or live.get("live_blocks") or live.get("thinking_active")
                     or live.get("pending_permission")
                     or live.get("meeting_participants")
                     or live.get("active_agents")):  # bg residual after a turn ended
            await self._send({"type": "live_state", **live, "chat_id": frame_chat_id})
            logger.info(f"WS dashboard: sent live_state after attach for chat={pump.chat_id}")

        try:
            while True:
                # Read client messages (non-blocking)
                try:
                    raw = await asyncio.wait_for(self.websocket.receive_text(), timeout=0.05)
                    client_msg = json.loads(raw)
                    cm_type = client_msg.get("type", "")

                    if cm_type == "permission_response":
                        if await self._may_resolve_permission(client_msg["request_id"]):
                            resolve_permission(client_msg["request_id"], client_msg.get("approved", True))
                            for sid, pd in list(_pending_permissions.items()):
                                if pd.get("request_id") == client_msg["request_id"]:
                                    del _pending_permissions[sid]
                                    break
                            await pump.resolve_active_permission()
                    elif cm_type == "question_response":
                        # Codex request_user_input answer — the held turn resumes.
                        # Validate the answerer drives this session, resolve the
                        # waiter with the answers map, then ADVANCE the pump's
                        # permission slot (else a later prompt in the same held turn
                        # buffers forever + a reconnect re-renders the answered card).
                        if await self._may_resolve_permission(client_msg["request_id"]):
                            resolve_question(
                                client_msg["request_id"], client_msg.get("answers") or {},
                            )
                            for sid, pd in list(_pending_permissions.items()):
                                if pd.get("request_id") == client_msg["request_id"]:
                                    del _pending_permissions[sid]
                                    break
                            await pump.resolve_active_permission()
                    elif cm_type == "location_response":
                        resolve_location(client_msg["request_id"], {
                            "lat": client_msg.get("lat"),
                            "lng": client_msg.get("lng"),
                            "accuracy": client_msg.get("accuracy"),
                            "error": client_msg.get("error"),
                        })
                    elif cm_type == "plan_review_response":
                        if not await self._may_resolve_permission(client_msg["request_id"]):
                            continue
                        action = client_msg.get("action", "")
                        plan_fn = client_msg.get("filename", "")
                        # Approve ExitPlanMode for implement AND reject (cancel).
                        # For "edit", deny so Claude stays in plan mode for revisions.
                        approved = action != "edit"
                        # Set session mode BEFORE resolve_permission so the hook
                        # endpoint sees the correct mode when it wakes up (prevents
                        # race where hook checks stale "plan" mode).
                        if approved and self.session_id:
                            if action == "reject":
                                set_session_mode(self.session_id, self.pre_plan_mode_holder[0])
                            elif action == "implement_accept_edits":
                                set_session_mode(self.session_id, "acceptEdits")
                            elif action == "implement_default":
                                set_session_mode(self.session_id, "default")
                        resolve_permission(client_msg["request_id"], approved)
                        for sid, pd in list(_pending_permissions.items()):
                            if pd.get("request_id") == client_msg["request_id"]:
                                del _pending_permissions[sid]
                                break
                        await pump.resolve_active_permission()
                        # Save the user's action in the DB turn block
                        req_id = client_msg.get("request_id", "")
                        for tb in pump._turn_blocks:
                            if tb.get("type") == "plan_review" and tb.get("request_id") == req_id:
                                tb["action"] = action
                                break
                        if self.chat_id and plan_fn and action == "reject":
                            task_store.update_chat_plan_status(self.chat_id, plan_fn, "rejected")
                            restored_mode = self.pre_plan_mode_holder[0]
                            task_store.update_chat(self.chat_id, permission_mode=restored_mode)
                            await self._send({"type": "mode_changed", "mode": restored_mode})
                        if action == "implement_accept_edits":
                            self.pending_control_requests.append(("set_permission_mode", {"mode": "acceptEdits"}))
                            task_store.update_chat(self.chat_id, permission_mode="acceptEdits")
                            await self._send({"type": "mode_changed", "mode": "acceptEdits"})
                            pump.queue_message("Please implement the plan now.")
                            pump.implementing_plan = plan_fn
                        elif action == "implement_default":
                            task_store.update_chat(self.chat_id, permission_mode="default")
                            await self._send({"type": "mode_changed", "mode": "default"})
                            pump.queue_message("Please implement the plan now.")
                            pump.implementing_plan = plan_fn
                    elif cm_type == "chat":
                        text = client_msg.get("text", "")
                        if text:
                            # Steer-first: engines that support it (Codex
                            # turn/steer) take the message INTO the running
                            # turn — delivered exactly-once on accept, so it
                            # must never also enter the queue. The user row
                            # persists immediately (it is part of this turn's
                            # context; the pump's turn blocks save after it,
                            # matching the interactive tailers' mid-turn user
                            # rows). Plan-implement enqueues use the
                            # plan_review branch above and never steer.
                            steered = False
                            if self.session_id and self.layer:
                                steered = bool(await self.layer.steer(self.session_id, text))
                            # Either way the message re-targets the end-of-turn
                            # alert to this device.
                            notification_manager.set_chat_turn_origin(
                                self.user_sub, pump.chat_id, self.notify_connection_id,
                            )
                            if steered:
                                task_store.add_chat_message(
                                    pump.chat_id, "user", text, author_sub=self.user_sub,
                                )
                                await self._send({"type": "steered", "text": text, "chat_id": frame_chat_id})
                            else:
                                idx = pump.queue_message(text)
                                await self._send({"type": "queued", "index": idx, "text": text, "chat_id": frame_chat_id})
                    elif cm_type == "artifact_interaction":
                        # display_ui backchannel mid-turn: QUEUE ONLY — page
                        # events never steer a running turn (lower authority
                        # than the user typing). Validation mirrors the
                        # between-turns handler.
                        from ws import artifact_interactions as _ai
                        a_token = str(client_msg.get("token") or "")
                        a_chat = str(client_msg.get("chat_id") or "")
                        a_frame: dict = {"type": "artifact_ack", "token": a_token}
                        if not a_chat or a_chat != pump.chat_id:
                            a_frame.update(status="denied", reason="not the viewed chat")
                        elif pump._meeting_agent or task_store.get_active_meeting_for_chat(a_chat):
                            a_frame.update(status="unavailable", reason="meeting in progress")
                        else:
                            interaction, a_err = _ai.validate_interaction(
                                a_chat, a_token,
                                str(client_msg.get("title") or ""),
                                client_msg.get("payload"),
                            )
                            if interaction is None:
                                a_frame.update(status="denied", reason=a_err)
                            elif not _ai.check_rate(a_chat, a_token):
                                a_frame.update(status="denied", reason="rate limited")
                            elif pump.queue_artifact(interaction):
                                a_frame["status"] = "queued"
                                notification_manager.set_chat_turn_origin(
                                    self.user_sub, pump.chat_id, self.notify_connection_id,
                                )
                            else:
                                a_frame.update(status="denied", reason="queue full")
                        await self._send(a_frame)
                    elif cm_type == "app_action":
                        # Mini-app send_prompt mid-turn: QUEUE ONLY — same
                        # never-steer rule as artifact interactions (the
                        # approved template doesn't upgrade page events to
                        # steering authority). Validation mirrors the
                        # between-turns handler.
                        from ws import artifact_interactions as _ai
                        ap_id = str(client_msg.get("app_id") or "")
                        ap_action = str(client_msg.get("action_id") or "")
                        ap_chat = str(client_msg.get("chat_id") or "")
                        ap_frame: dict = {"type": "app_action_ack", "app_id": ap_id,
                                          "action_id": ap_action}
                        if not ap_chat or ap_chat != pump.chat_id:
                            ap_frame.update(status="denied", reason="not the viewed chat")
                        elif pump._meeting_agent or task_store.get_active_meeting_for_chat(ap_chat):
                            ap_frame.update(status="unavailable", reason="meeting in progress")
                        else:
                            interaction, ap_err = _ai.validate_app_action(
                                ap_chat, self.agent_name or "", self.user_sub or "",
                                ap_id, ap_action, client_msg.get("args"),
                            )
                            if interaction is None:
                                ap_frame.update(status="denied", reason=ap_err)
                            elif not _ai.check_rate(ap_chat, f"app:{ap_id}"):
                                ap_frame.update(status="denied", reason="rate limited")
                            elif pump.queue_artifact(interaction):
                                ap_frame["status"] = "queued"
                                notification_manager.set_chat_turn_origin(
                                    self.user_sub, pump.chat_id, self.notify_connection_id,
                                )
                            else:
                                ap_frame.update(status="denied", reason="queue full")
                        await self._send(ap_frame)
                    elif cm_type == "cancel_queued":
                        idx = client_msg.get("index", -1)
                        text = pump.cancel_queued(idx)
                        if text is not None:
                            await self._send({"type": "queue_removed", "index": idx, "text": text, "chat_id": frame_chat_id})
                    elif cm_type == "cancel_all_queued":
                        combined = pump.cancel_all_queued()
                        await self._send({"type": "queue_cleared", "text": combined, "chat_id": frame_chat_id})
                    elif cm_type == "abort":
                        logger.info(f"WS dashboard: abort via pump, session={self.session_id}")
                        # Layer abort FIRST: on the graceful path (Claude
                        # control_request interrupt / Codex turn/interrupt) the
                        # producer is the sole consumer of the closing turn's
                        # tail events and must stay alive — the pump runs to
                        # PRODUCER_DONE and persists the partial turn; the CLI
                        # layer's watchdog falls back to killpg if the turn
                        # doesn't close. Hard path keeps today's cancel order.
                        graceful = False
                        if self.session_id and self.layer:
                            graceful = bool(await self.layer.abort(self.session_id))
                        # Queued messages never survive an abort (the user asked
                        # everything to stop): without the clear, the graceful
                        # producer's post-turn drain would run them as new turns
                        # and the hard path silently dropped them (pre-existing).
                        _dropped_q = pump.cancel_all_queued()
                        if _dropped_q:
                            await self._send({"type": "queue_cleared",
                                              "text": _dropped_q,
                                              "chat_id": frame_chat_id})
                        if not graceful:
                            pump.abort()
                        pump.detach(ws_queue)
                        # Kill process but keep session entry for auto-resume.
                        # Next send_message() detects dead process -> auto-resume.
                        # Don't clear session_id — it stays set for the next message.
                        if self.session_id:
                            _pending_permissions.pop(self.session_id, None)
                        self.implementing_plan = ""
                        # Mark the cancelled turn: the scheduler/delegate layer
                        # derives user_interrupted from last_turn_aborted on
                        # EVERY abort path; the graceful flag additionally
                        # suppresses the next turn's cancelled-context
                        # injection (the engine's own history has the partial
                        # turn).
                        if self.chat_id:
                            task_store.update_chat(self.chat_id,
                                                   last_turn_aborted=True,
                                                   last_abort_graceful=graceful)
                            # A hard CLI Stop kills the whole process group —
                            # any bg agents/commands died with it; clear their
                            # badges. Graceful keeps the process (and its bg
                            # work) alive; Codex keeps its daemon either way.
                            if self.session_id and not graceful:
                                _abort_chat = task_store.get_chat(self.chat_id)
                                _abort_path = resolve_execution_path(
                                    self.agent_name,
                                    (_abort_chat or {}).get("execution_path", ""),
                                )
                                if _abort_path == "claude-code-cli":
                                    clear_session_liveness(self.session_id, reason="abort")
                        await self._send({"type": "aborted", "chat_id": frame_chat_id})
                        self.pending_control_requests.clear()
                        break
                    elif cm_type == "resume_chat":
                        logger.info(f"WS dashboard: resume_chat during pump streaming, detaching")
                        pump.detach(ws_queue)
                        self.pending_control_requests.clear()
                        result["detached"] = True
                        result["resume_msg"] = client_msg
                        break
                    elif cm_type == "mode_change":
                        await self._handle_mode_change(client_msg)
                    elif cm_type == "model_change":
                        await self._handle_model_change(client_msg)
                    elif cm_type == "ping":
                        await self._send({"type": "pong"})
                    elif cm_type == "close":
                        pump.detach(ws_queue)
                        result["detached"] = True
                        break
                    elif cm_type in ("warmup", "pre_warmup"):
                        # Chat-switch intent for a DIFFERENT chat (e.g. the user
                        # opened a new chat and sent its first message while this
                        # chat is still generating). Detach — the pump keeps
                        # running headless — and hand the message back to the
                        # main loop for normal dispatch. These were previously
                        # swallowed here: the new chat's first turn was lost and
                        # this chat's frames kept rendering into the new view.
                        logger.info(f"WS dashboard: {cm_type} during pump streaming — detach + dispatch")
                        pump.detach(ws_queue)
                        self.pending_control_requests.clear()
                        result["detached"] = True
                        result["resume_msg"] = client_msg
                        break
                    elif cm_type == "user_active":
                        notification_manager.set_connection_active(self.user_sub, self.notify_connection_id, True)
                    elif cm_type == "user_idle":
                        notification_manager.set_connection_active(
                            self.user_sub, self.notify_connection_id, False,
                            away=bool(client_msg.get("away")),
                        )
                    elif cm_type == "chat_read":
                        # Read receipts arrive mid-turn too (the client marks
                        # the chat read on every history load) — same handler
                        # as between turns; DB + fan-out only, stream-safe.
                        await self._handle_chat_read(client_msg)
                    else:
                        logger.warning(
                            f"WS dashboard: unhandled client message type={cm_type!r} during pump streaming — dropped"
                        )
                except asyncio.TimeoutError:
                    pass
                except (WebSocketDisconnect, RuntimeError):
                    logger.info(f"WS dashboard: WS disconnected during pump streaming")
                    pump.detach(ws_queue)
                    result["detached"] = True
                    break
                except json.JSONDecodeError:
                    pass

                # Read from pump's event queue
                try:
                    item = await asyncio.wait_for(ws_queue.get(), timeout=0.15)
                except asyncio.TimeoutError:
                    continue

                pt = item.get("pump_type", "")

                if pt == "ws_event":
                    event = item["event"]
                    # Plan mode: track pre-plan mode for restoration
                    if event.get("type") == "plan_mode":
                        if event.get("action") == "enter":
                            self.pre_plan_mode_holder[0] = get_session_mode(self.session_id) or "default"
                        # session mode is already set by the pump
                    await self._send({**event, "chat_id": frame_chat_id})

                elif pt == "perm_permission_prompt":
                    perm_data = item["perm_data"]
                    event = {"type": "permission_prompt",
                             "request_id": perm_data["request_id"],
                             "tool_name": perm_data["tool_name"],
                             "tool_input": perm_data.get("tool_input", {}),
                             "chat_id": frame_chat_id}
                    if item.get("meeting_agent"):
                        event["meeting_agent"] = item["meeting_agent"]
                    await self._send(event)

                elif pt == "perm_plan_review":
                    perm_data = item["perm_data"]
                    await self._send({"type": "plan_review",
                                 "request_id": perm_data["request_id"],
                                 "plan": perm_data.get("plan", ""),
                                 "tool_input": perm_data.get("tool_input", {}),
                                 "filename": item.get("filename", ""),
                                 "chat_id": frame_chat_id})

                elif pt == "perm_question_prompt":
                    # Codex request_user_input → the dashboard question card. Unlike
                    # Claude's fire-and-forget `question` (answer = a fresh chat turn),
                    # this carries a request_id: the held turn resumes only when the
                    # FE answers via `question_response`.
                    perm_data = item["perm_data"]
                    await self._send({"type": "question",
                                 "request_id": perm_data["request_id"],
                                 "tool_name": perm_data.get("tool_name", "request_user_input"),
                                 "tool_input": perm_data.get("tool_input", {}),
                                 "chat_id": frame_chat_id})

                elif pt == "perm_mode_restored":
                    await self._send({"type": "mode_changed", "mode": item["mode"], "chat_id": frame_chat_id})

                elif pt == "queue_turn":
                    await self._send({"type": "queue_sent", "text": item["text"], "chat_id": frame_chat_id})

                elif pt == "artifact_interaction":
                    # Drained backchannel interaction — the transcript chip
                    # renders at delivery time (the row is already persisted).
                    await self._send({
                        "type": "artifact_interaction",
                        "token": item.get("token", ""),
                        "title": item.get("title", ""),
                        "payload": item.get("payload"),
                        "chat_id": frame_chat_id,
                    })

                elif pt == "app_action":
                    # Drained mini-app send_prompt action — same chip-at-
                    # delivery contract as artifact_interaction.
                    await self._send({
                        "type": "app_action",
                        "app_id": item.get("app_id", ""),
                        "slug": item.get("slug", ""),
                        "title": item.get("title", ""),
                        "action_id": item.get("action_id", ""),
                        "label": item.get("label", ""),
                        "prompt": item.get("prompt", ""),
                        "chat_id": frame_chat_id,
                    })

                elif pt == "is_done":
                    pass  # Turn boundary — pump already saved to DB

                elif pt == "all_done":
                    await self._send({"type": "done", "chat_id": frame_chat_id})
                    break

                elif pt == "error":
                    await self._send({"type": "error", "message": item["message"], "chat_id": frame_chat_id})
                    break

                elif pt in ("detached", "pump_ended"):
                    # Another WS took over, or the pump finished/died. A
                    # pump_ended seen HERE means this loop never consumed
                    # all_done (it attached after the producer's final flush —
                    # e.g. a mid-turn re-attach racing the turn's end), so the
                    # client never got its `done`: always send it. A loop that
                    # did see all_done broke at that branch and never reads
                    # pump_ended, so this cannot double-send.
                    if pt == "pump_ended":
                        await self._send({"type": "done", "chat_id": frame_chat_id})
                    result["detached"] = True
                    break

        finally:
            self.streaming = False

        return result

    async def _enter_pump_loop(self) -> ChatStreamPump | None:
        """If there's an active pump for the current chat, attach and stream.

        Handles chat switching (resume_chat) within the loop. Returns the
        last pump that was streamed, or None if no pump was active.

        For multi-turn task chats: after a turn's pump finishes, re-sends
        chat_history with all turns' messages, waits briefly for the next
        turn's pump, and attaches if one appears. When a pump from a
        different chat_id (new turn) is found, re-sends history first so
        the frontend has the new turn's user message.
        """
        last_pump = None
        task_wait_retries = 0
        last_pump_chat_id: str | None = None
        # Bounded so a pathological promise→die→promise→die interleave can't
        # spin; in practice one re-send converges (the turn is persisted).
        history_resend_budget = 2
        while True:
            pump = _active_pumps.get(self.chat_id)
            # Never attach to an active external session's pump (phone/website/
            # webhook): attach() would steal its stream and kill the live call.
            # These are viewed read-only via chat_history (_handle_resume_chat).
            if pump and pump.source_type in _EXTERNAL_DRIVEN_SOURCES:
                pump = None
            if (not pump or pump.is_done):
                pump = self._find_task_pump()
            if not pump or pump.is_done:
                # Wait briefly for next pump: task chats (multi-turn) OR meetings
                is_meeting = bool(task_store.get_active_meeting_for_chat(self.chat_id))
                max_retries = 15 if is_meeting else 5  # 30s for meetings, 10s for tasks
                should_poll = (
                    last_pump and task_wait_retries < max_retries and (
                        (self.chat_id and self.chat_id.startswith("task-"))
                        or is_meeting
                    )
                )
                if should_poll:
                    task_wait_retries += 1
                    await asyncio.sleep(2)
                    continue
                # Resume promised an attach for this chat (its history went out
                # TRUNCATED at the pump's cutoff) but the pump finished in the
                # gap before we got here: the final turn's rows are missing
                # client-side and no live_state/done will ever arrive. Re-send
                # fresh history — the turn is persisted by now (_save_turn_blocks
                # runs before the pump is removed). A new pump may re-promise
                # (we attach next pass); otherwise the next pass breaks clean.
                if (self.chat_id and self.promised_pump_chat == self.chat_id
                        and history_resend_budget > 0):
                    history_resend_budget -= 1
                    self.promised_pump_chat = None
                    logger.info(
                        f"WS dashboard: promised pump gone before attach for "
                        f"chat={self.chat_id} — re-sending history"
                    )
                    await self._handle_resume_chat({"chat_id": self.chat_id})
                    continue
                break

            task_wait_retries = 0
            self.promised_pump_chat = None  # attaching now — promise kept

            # New turn's pump found (different chat_id) — re-send history
            # so the frontend gets the new turn's user message before streaming
            if (pump.chat_id != last_pump_chat_id and last_pump_chat_id is not None
                    and self.chat_id and self.chat_id.startswith("task-")):
                await self._handle_resume_chat({"chat_id": self.chat_id})

            last_pump_chat_id = pump.chat_id
            last_pump = pump
            result = await self._stream_via_pump(pump)

            if result.get("resume_msg"):
                rm = result["resume_msg"]
                if rm.get("type") == "resume_chat":
                    prev_chat, prev_sid = self.chat_id, self.session_id
                    await self._handle_resume_chat(rm)
                    # Only a SAME-chat resume supersedes the old sid — on a
                    # chat switch the old entry still serves the backgrounded
                    # chat's server kicks.
                    self._register_notify_queue(
                        replaces=prev_sid
                        if rm.get("chat_id") == prev_chat else "",
                    )
                else:
                    # warmup / pre_warmup that escaped the streaming loop —
                    # route through the normal dispatcher (it backgrounds
                    # pre_warmup and runs warmup's full path).
                    await self._dispatch_client_message(rm)
                continue

            # For task chats and meetings: after a turn finishes, re-send
            # chat_history, then loop to wait for the next pump.
            if not result.get("detached") and (
                (self.chat_id and self.chat_id.startswith("task-"))
                or task_store.get_active_meeting_for_chat(self.chat_id)
            ):
                await self._handle_resume_chat({"chat_id": self.chat_id})
                continue

            # Normal exit (done, abort, close, WS disconnect)
            if not result.get("detached"):
                await self._flush_pending_control_requests()
                if pump._plan_filename:
                    # Carry to the next pump so plan edits keep updating the
                    # SAME plan file across turns (was a dead closure local —
                    # only the resume-from-DB path ever restored it).
                    self.chat_plan_filename = pump._plan_filename
                if pump.implementing_plan and not pump.message_queue:
                    task_store.update_chat_plan_status(
                        self.chat_id, pump.implementing_plan, "implemented",
                    )
                    await self._send({"type": "plan_status",
                                 "filename": pump.implementing_plan,
                                 "status": "implemented"})
                    pump.implementing_plan = ""
            # Background subagents still running after the turn returned → watch
            # for their (deterministic) completion + nudge. Launched even on
            # detach (another tab) — the monitor's own per-session running-guard
            # dedups, so whichever owner gets here first wins and the nudge fires
            # exactly once. Pending count from the SubagentRegistry, not a FIFO.
            self._ensure_bg_monitor()
            break

        return last_pump

    def _find_task_pump(self) -> ChatStreamPump | None:
        """For multi-turn task chats, find an active pump on any related run.

        Multi-turn delegates share a session_id but each turn has its own
        chat_id (task-{runId}). The user views the first run's chat, but the
        active pump may be on a later turn's chat_id.
        """
        if not self.chat_id or not self.chat_id.startswith("task-"):
            return None
        run_id = self.chat_id.removeprefix("task-")
        run = task_store.get_run(run_id)
        if not run or not run.get("session_id"):
            return None
        related = task_store.list_runs(limit=50, session_id=run["session_id"])
        for r in related:
            if r.get("chat_id") and r["chat_id"] != self.chat_id:
                p = _active_pumps.get(r["chat_id"])
                if p and not p.is_done:
                    return p
        return None

    def _ensure_bg_monitor(self,
        session: str | None = None,
        chat: str | None = None,
        layer_: "ExecutionLayer | None" = None,
    ) -> None:
        """Launch the bg-agent monitor for a session's cohort if it has pending
        background subagents and one isn't already running (idempotent via the
        monitor's per-session guard). Defaults to the connection's VIEWED
        session/chat/layer (turn-end + reconnect); a headless server turn passes
        its own explicit (session, chat, layer) so a backgrounded review/synthesis
        turn on a non-viewed chat still gets its review nudge."""
        sid = session or self.session_id
        cid = chat or self.chat_id
        lyr = layer_ or self.layer
        if not (sid and cid and lyr):
            return
        reg = get_subagent_registry(sid)
        if reg.has_pending and not bg_monitor_running(sid):
            asyncio.create_task(
                _bg_agent_monitor(lyr, sid, cid, reg.pending_count)
            )
        # Background bash commands have no completion hook — their monitor reads
        # stdout post-turn to detect completion + nudge (separate cohort, mirror).
        bgreg = get_bg_command_registry(sid)
        if bgreg.has_pending and not bg_command_monitor_running(sid):
            asyncio.create_task(
                _bg_command_monitor(lyr, sid, cid, bgreg.pending_count)
            )

    # Per-chat live-dot broadcasts (chat_status) are emitted by the PUMP itself
    # on every turn start/end — viewed, detached, or headless — via
    # notification_manager.broadcast_chat_status. No per-connection emission
    # needed here anymore.

    async def _arm_bg_monitor_after(self,
        pump: ChatStreamPump, sid: str, cid: str, lyr,
    ) -> None:
        """Await a pump's completion, then arm the bg-agent/command monitors for
        its (session, chat, layer). Spawned by _start_new_stream for EVERY turn
        (non-blocking), so a turn that leaves background subagents or commands
        running gets its badges cleared + review nudge even when the viewer
        detached mid-turn. Awaiting the pump task (not the turn's last event)
        matters: it completes only after the producer's finally ran, which is
        where Codex registers bg sub-agents that outlive the turn — checking
        has_pending any earlier can miss them."""
        try:
            if pump._task:
                await pump._task
        except (asyncio.CancelledError, Exception):
            pass
        self._ensure_bg_monitor(session=sid, chat=cid, layer_=lyr)

    def _deterministic_title(self, text: str) -> str:
        """Stable chat title from the first user message — first ~6 words / 48 chars,
        whitespace-collapsed, ellipsis if truncated. No LLM, no post-turn rename
        (replaces the old OpenAI title generator that caused the "New Chat" → rename
        churn). Interactive sends reach here with the injected ``[Current time:
        ...]`` stamp already prepended — drop it or it becomes the title. A
        mini-app action's framed prompt titles as "App — Label" instead of the
        raw framing brackets (twin recognizer in transcript_tailer)."""
        stripped = _TIME_PRELUDE_RE.sub("", text or "", count=1)
        m = _APP_ACTION_HEADER_RE.match(stripped)
        if m:
            stripped = f"{m.group(1)} — {m.group(2)}"
        cleaned = " ".join(stripped.split())
        if not cleaned:
            return "New Chat"
        words = cleaned.split(" ")
        title = " ".join(words[:6])
        cut = len(words) > 6
        if len(title) > 48:
            title = title[:48].rstrip()
            cut = True
        return title + ("…" if cut else "")

    def _persist_first_prompt(self, cid: str, prompt_text: str) -> None:
        """Persist the first user prompt + a deterministic title at send-time so
        the chat is durable during the spawn window and the sidebar shows a
        stable name immediately. The turn is server-kicked after
        warmup_ready via _handle_chat(_server_kick=True), which skips re-persist.
        Image/file attachment meta for the first prompt is captured at turn time."""
        if not cid or not prompt_text:
            return
        task_store.add_chat_message(cid, "user", prompt_text, author_sub=self.user_sub)
        # Interactive spawns: the CLI journals this exact text and the tailer
        # would re-insert it — note it so the tailer skips that one row (the
        # live-observed duplicated first user row). Harmless for headless
        # (nothing consumes the note; TTL-pruned).
        from core.session.transcript_tool_events import note_sent_prompt
        note_sent_prompt(cid, prompt_text)
        # Route the end-of-turn alert to the device that sent this prompt.
        notification_manager.set_chat_turn_origin(self.user_sub, cid, self.notify_connection_id)
        rec = task_store.get_chat(cid)
        if rec and not rec.get("title"):
            task_store.update_chat(cid, title=self._deterministic_title(prompt_text))

    def _build_cancelled_context(self, cid: str) -> str:
        """Read the cancelled turn's messages from DB and format for injection.

        The current user message was JUST saved before this is called.
        Walk backwards to find the previous user message (the cancelled one)
        and any partial assistant response after it.
        """
        messages = task_store.get_chat_messages(cid)
        if not messages:
            return ""

        user_count = 0
        last_user_text = ""
        assistant_parts = []

        for msg in reversed(messages):
            if msg["role"] == "user" and msg["content"]:
                user_count += 1
                if user_count == 1:
                    continue  # Skip the new message (just saved)
                last_user_text = msg["content"]
                break
            elif msg["role"] == "assistant" and msg["content"]:
                if user_count >= 1:
                    assistant_parts.insert(0, msg["content"])
            elif msg["role"] == "event":
                continue

        if not last_user_text:
            return ""

        parts = [
            "[Your previous response was cancelled by the user. "
            "The cancelled turn was not saved to your session context, "
            "so here is what happened:]",
            f"User said: {last_user_text}",
        ]
        if assistant_parts:
            combined = "\n".join(assistant_parts)
            if len(combined) > 2000:
                combined = combined[:2000] + "\n... (response was truncated)"
            parts.append(f"Your partial response before cancellation:\n{combined}")
        else:
            parts.append("(You had not started responding yet when cancelled)")
        parts.append("[End of cancelled context. The user's new message follows below.]")

        return "\n".join(parts)

    async def _process_attachments(self,
        text: str, images: list[dict], files: list[dict], *,
        agent: str, agent_dir, is_agent_scoped: bool, username: str,
        is_direct_llm: bool,
    ) -> tuple[str, list[dict], list[dict], list[dict]]:
        """Save chat-attached photos/files to the agent's scope-correct
        workspace and build the turn payload.

        Returns ``(cli_text, attached_images, image_meta, valid_files)``:
        ``cli_text`` is the prompt with sandbox-virtual paths injected for
        CLI/Codex (their built-in Read tool opens them); ``attached_images`` are
        base64 vision blocks for Direct LLM (no Read tool). Shared by the normal
        user turn (``_handle_chat``) and the headless first-turn server-kick
        (when the user navigated away during the spawn) so both save to the SAME
        agent workspace and inject identical paths. The agent only ever sees
        sandbox-virtual paths (`/users/{u}/...` or `/workspace/...`) — local
        sandboxes resolve them via bwrap mounts, remote satellites translate
        them via `satellite/path_translator.translate_paths_in_text`."""
        image_meta: list[dict] = []
        attached_images: list[dict] = []  # for Direct LLM content blocks
        cli_text = text  # text sent to CLI (may include image paths for CLI/Codex)
        if images:
            # Dedicated subfolder for chat-attached photos so the workspace
            # root stays tidy. Mirrors image-gen-mcp's `generated-assets/` and
            # the chat-file path's `uploads/files/`. Lazy mkdir on first use
            # via `_save_base64_image` -> `save_dir.mkdir(parents=True, exist_ok=True)`.
            if is_agent_scoped:
                img_dir = agent_dir / "workspace" / "uploads" / "photos"
            else:
                img_dir = agent_dir / "users" / username / "workspace" / "uploads" / "photos"
            saved_images: list[dict] = []  # each: {"path", "base64", "media_type"}
            for img in images:
                data_url = img.get("data", "")
                if not data_url:
                    continue
                # Ensure data URL format
                if not data_url.startswith("data:"):
                    data_url = f"data:image/jpeg;base64,{data_url}"
                saved = _save_base64_image(data_url, save_dir=img_dir)
                if saved:
                    saved_images.append(saved)
                    image_meta.append({
                        "name": img.get("name", "photo.jpg"),
                        # agent-relative saved path — after a reload the
                        # frontend renders the photo via
                        # GET /v1/agents/<agent>/files/<path> (the base64
                        # data URL only exists on the live send).
                        "path": str(
                            Path(saved["path"]).resolve().relative_to(
                                agent_dir.resolve())),
                    })

            if saved_images:
                # Push freshly-saved photos to any active remote satellite
                # session for this agent. Mirrors api/media/uploads.py — without
                # this, the satellite-side CLI tries to Read the path before
                # end-of-turn sync ever runs and sees ENOENT.
                from api.media.uploads import _push_upload_to_active_remote_sessions
                for s in saved_images:
                    try:
                        host_path = Path(s["path"])
                        rel_path = str(host_path.relative_to(agent_dir))
                        await _push_upload_to_active_remote_sessions(
                            agent, rel_path, host_path,
                        )
                    except Exception:
                        logger.exception("Photo push to satellite failed: %s", s["path"])

                if is_direct_llm:
                    # Direct LLM agents have no Read tool — attach images as
                    # native vision content blocks via `images` kwarg into
                    # `send_message` / `run_direct_stream`. Skip path-injection
                    # text entirely; the LLM sees the image in the message body.
                    for s in saved_images:
                        attached_images.append({
                            "base64": s["base64"],
                            "media_type": s["media_type"],
                        })
                else:
                    # CLI / Codex: inject sandbox-virtual path so the agent's
                    # built-in Read tool can open the file from disk.
                    cli_text += f"\n\nThe user has attached {len(saved_images)} image(s). Read and analyze them using the Read tool:\n"
                    for s in saved_images:
                        sandbox_path = _host_to_sandbox_path(s["path"], agent_dir)
                        cli_text += f"- {sandbox_path}\n"

        # Validate and inject attached files. Files arrive with agent-relative
        # paths (e.g. `users/alice/workspace/uploads/files/foo.pdf` for user-
        # scoped, `workspace/uploads/files/foo.pdf` for agent-scoped — set by
        # the upload endpoint based on `is_shared_only(agent)`). Validate the path
        # is within the chat's expected scope, then inject as sandbox-virtual
        # (leading `/`).
        valid_files: list[dict] = []
        if files:
            from api.media.uploads import FILE_TYPE_LABELS
            if is_agent_scoped:
                expected_prefix = "workspace/"
            else:
                expected_prefix = f"users/{username}/workspace/"
            expected_root = (agent_dir / expected_prefix).resolve()
            for f in files:
                fpath = f.get("path", "")
                fname = f.get("name", "")
                if fpath.startswith(expected_prefix) and fname:
                    # The prefix check alone would let `..` segments escape
                    # (e.g. `workspace/../../x`) and turn is_file() into a
                    # host-file existence oracle — require the resolved path
                    # to stay inside the scope root.
                    try:
                        full = (agent_dir / fpath).resolve()
                        if not full.is_relative_to(expected_root):
                            continue
                    except OSError:
                        continue
                    if full.is_file():
                        valid_files.append({"path": fpath, "name": fname})
            if valid_files:
                cli_text += f"\n\nThe user has attached {len(valid_files)} file(s):\n"
                for vf in valid_files:
                    ext = Path(vf["name"]).suffix.lower()
                    label = FILE_TYPE_LABELS.get(ext, "File")
                    cli_text += f"- /{vf['path']} ({label})\n"
                cli_text += "\nRead the file(s) using the Read tool to see their contents.\n"
        return cli_text, attached_images, image_meta, valid_files

    async def _flush_pending_control_requests(self):
        """Send queued control requests via the execution layer after a streaming turn ends."""
        if not self.pending_control_requests:
            return
        if not self.session_id or not self.layer:
            self.pending_control_requests.clear()
            return
        # Filter to only commands this layer supports
        caps = self.layer.capabilities
        supported = set(caps.control_commands) if caps.supports_control_commands else set()
        try:
            async with self.layer.session_lock(self.session_id):
                for subtype, kwargs in self.pending_control_requests:
                    if subtype not in supported:
                        logger.debug(f"Skipping unsupported control_request {subtype} for {caps.name}")
                        continue
                    try:
                        await self.layer.send_control_request(self.session_id, subtype, **kwargs)
                    except Exception as e:
                        logger.warning(f"Deferred control_request {subtype} error: {e}")
            self.pending_control_requests.clear()
        except Exception as e:
            logger.warning(f"Failed to flush control requests: {e}")
            self.pending_control_requests.clear()

    async def _may_resolve_permission(self, request_id: str) -> bool:
        """Whether THIS connection may answer a permission/plan prompt.

        A permission response drives the agent (approves tool execution /
        flips session mode), so it must come from the connection actually
        viewing the session the request is bound to — attaching to a chat
        already passed the per-chat access gates — and never for a task run
        the user can't continue (viewers of an agent-scoped run may watch
        the stream but not advance it). A meeting agent-session's prompts
        are shown on its pump chat, so those resolve against the meeting's
        pump/parent session ids. Requests recorded without a session keep
        legacy behavior (nothing to bind against).
        """
        bound_sid = get_permission_request_session(request_id)
        if bound_sid is None:
            return True
        allowed_sids = {bound_sid}
        meeting = get_meeting_session_info(bound_sid)
        if meeting:
            allowed_sids.add(meeting.get("pump_session_id") or "")
            allowed_sids.add(meeting.get("parent_session_id") or "")
        if self.session_id not in allowed_sids:
            logger.warning(
                "WS dashboard: dropped permission response for request %s — "
                "bound to a different session than this connection views",
                request_id[:8],
            )
            return False
        if await self._deny_task_continue(self.chat_id):
            return False
        return True

    async def _deny_task_continue(self, cid: str | None) -> bool:
        """Enforce the task continue-gate for a ``task-{run_id}`` chat.

        Sends an error + returns True when the user may NOT continue the run
        (agent-scoped → editor+; user-scoped → creator/admin — see
        ``_task_continue_allowed``). No-op (returns False) for non-task chats.
        Called at every entry point that (re)warms or drives a task session.
        """
        if not cid or not cid.startswith("task-"):
            return False
        run = task_store.get_run(cid.removeprefix("task-"))
        if not run:
            await self._send_error("Task run not found")
            return True
        eff_role = _effective_agent_role(
            self.user_sub, run.get("agent") or "", fallback_user=self.user,
        )
        if not _task_continue_allowed(run, effective_role=eff_role, user_sub=self.user_sub):
            await self._send_error("Access denied")
            return True
        return False

    async def _handle_mode_change(self, msg: dict):
        new_mode = msg.get("mode", "")
        # Validate against layer's supported permission modes (if available)
        valid_modes = {"default", "acceptEdits", "plan", "dontAsk"}
        if self.layer:
            caps = self.layer.capabilities
            if caps.permission_modes:
                valid_modes = set(caps.permission_modes)
        if new_mode not in valid_modes:
            await self._send_error(f"Invalid mode: {new_mode}")
            return

        # Pre-warmed session (before first message): apply mode to pre-warmed session
        sid = self.session_id or self._pre_warmed_sid
        if not sid or not self.layer:
            # No session yet — defer until warmup creates one
            self.deferred_mode = new_mode
            await self._send({"type": "mode_changed", "mode": new_mode})
            return
        old_mode = get_session_mode(sid) or "default"

        # Always update session mode in memory — meeting agents' hooks
        # check get_session_mode(parent_session_id) and need this even
        # when the parent CLI process is dead.
        set_session_mode(sid, new_mode)
        if self.chat_id:
            task_store.update_chat(self.chat_id, permission_mode=new_mode)

        if not await self.layer.is_session_alive(sid):
            self.deferred_mode = new_mode
            await self._send({"type": "mode_changed", "mode": new_mode})
            return
        await self._send({"type": "mode_changed", "mode": new_mode})
        logger.info(f"WS dashboard mode changed: session={self.session_id}, mode={new_mode}, old={old_mode}, streaming={self.streaming}")

        # Exiting plan mode via dropdown: approve any pending ExitPlanMode
        # permission so the CLI actually exits plan mode internally
        if old_mode == "plan" and new_mode != "plan":
            if self.session_id in _pending_permissions:
                pd = _pending_permissions[self.session_id]
                if pd.get("event_type") == "plan_review":
                    resolve_permission(pd["request_id"], True)
                    del _pending_permissions[self.session_id]
                    pump = _active_pumps.get(self.chat_id)
                    if pump:
                        await pump.resolve_active_permission()
                    logger.info(f"WS dashboard: auto-approved ExitPlanMode for mode change to {new_mode}")

        # Apply mode change via execution layer control channel (if supported)
        caps = self.layer.capabilities
        if "set_permission_mode" in (caps.control_commands if caps.supports_control_commands else []):
            if self.streaming:
                self.pending_control_requests.append(("set_permission_mode", {"mode": new_mode}))
            else:
                try:
                    await self.layer.change_mode(sid, new_mode)
                except Exception as e:
                    logger.warning(f"Mode change error: {e}")
        # Note: session_state mode is always set (above) regardless of control
        # support — the hook system uses session_state, not CLI's internal mode.

    async def _handle_model_change(self, msg: dict):
        new_model = msg.get("model", "")
        if not new_model:
            await self._send_error("Model required")
            return

        # Refuse a model foreign to this chat's execution layer (see
        # _model_allowed_for_path) and resync the client's selector to the
        # chat's real model instead of applying/persisting the poison.
        if self.chat_id:
            chat_rec = task_store.get_chat(self.chat_id) or {}
            chat_path = chat_rec.get("execution_path") or resolve_execution_path(
                chat_rec.get("agent") or self.agent_name or ""
            )
            if not _model_allowed_for_path(new_model, chat_path):
                logger.warning(
                    f"WS dashboard model change REFUSED: model={new_model} is not a "
                    f"{chat_path} model (chat={self.chat_id}) — keeping {chat_rec.get('model', '')!r}"
                )
                await self._send({
                    "type": "model_changed",
                    "model": chat_rec.get("model", ""),
                    "chat_id": self.chat_id,
                })
                return

        # Persist to DB immediately (even before session exists)
        if self.chat_id:
            task_store.update_chat(self.chat_id, model=new_model)
        await self._send({"type": "model_changed", "model": new_model})

        if not self.session_id or not self.layer:
            # No session yet — store for when session is created via warmup
            self.deferred_model = new_model
            logger.info(f"WS dashboard model deferred: model={new_model} (no session yet)")
            return

        if not await self.layer.is_session_alive(self.session_id):
            self.deferred_model = new_model
            logger.info(f"WS dashboard model deferred: model={new_model} (session not found)")
            return

        logger.info(f"WS dashboard model changed: session={self.session_id}, model={new_model}, streaming={self.streaming}")

        # Apply model change via execution layer
        caps = self.layer.capabilities
        if "set_model" in (caps.control_commands if caps.supports_control_commands else []) and self.streaming:
            # CLI path while streaming — queue for control channel
            self.pending_control_requests.append(("set_model", {"model": new_model}))
        else:
            # Direct change: CLI (not streaming) or direct-llm (always)
            try:
                await self.layer.change_model(self.session_id, new_model)
            except Exception as e:
                logger.warning(f"Model change error: {e}")

    async def _handle_execution_mode_change(self, msg: dict):
        """Persist the per-chat interactive toggle.

        This handler is persist-only: it writes ``chats.execution_mode``
        ('interactive' or '' for headless ``-p``) so the choice survives a
        reload/resume before the next send (the warmup then spawns the chosen
        mode). It deliberately does NOT touch a live session — switching an
        already-running chat is the kill+rewarm, and the dashboard locks
        the toggle while a session is live. chat_id comes from the message (a
        reopened dead chat may not have bound the connection's chat_id yet),
        falling back to the connection's bound chat_id."""
        # Accepted: "interactive" (on), "-p" (explicit headless — OVERRIDES an
        # interactive per-agent default, which "" cannot, since "" falls through to
        # the agent default in the resolver), "" (unset → follow the default).
        new_mode = msg.get("execution_mode", "") or ""
        if new_mode not in ("", "interactive", "-p"):
            await self._send_error(f"Invalid execution_mode: {new_mode}")
            return
        cid = msg.get("chat_id") or self.chat_id
        if cid:
            task_store.update_chat(cid, execution_mode=new_mode)
        await self._send({"type": "execution_mode_changed", "execution_mode": new_mode})

    async def _handle_switch_execution_mode(self, msg: dict):
        """Live toggle: switch a LIVE chat
        between interactive and headless ``-p``. Kills the current session
        (keeping the JSONL → resumable), reloads the conversation into the client,
        then re-warms in the target mode resuming the same conversation. The
        dashboard confirms first + defers while a ``-p`` turn streams; the
        interactive ``close()`` tails the transcript so the swapped-in ``-p``
        history is populated. Falls back to persist-only when nothing is live."""
        # Accepted: "interactive" (on) + "-p" (explicit headless — what the toggle
        # sends for OFF, to override an interactive per-agent default; "" can't,
        # it resolves back to the agent default) + "" (unset). Without "-p"
        # here the OFF switch was rejected (WS error, no re-warm) → the terminal
        # stayed and nothing happened — for BOTH Claude and Codex.
        new_mode = msg.get("execution_mode", "") or ""
        if new_mode not in ("", "interactive", "-p"):
            await self._send_error(f"Invalid execution_mode: {new_mode}")
            return
        cid = msg.get("chat_id") or self.chat_id
        chat = task_store.get_chat(cid) if cid else None
        if not cid or not chat:
            await self._handle_execution_mode_change(msg)  # nothing live → just persist
            return

        # Persist the target so the re-warm resolves to it.
        task_store.update_chat(cid, execution_mode=new_mode)
        old_sid = chat.get("session_id") or (self.session_id if cid == self.chat_id else None)
        agent = chat.get("agent", "")
        role = _effective_agent_role(self.user_sub, agent, fallback_user=self.user)

        # Kill the current live session, keeping the conversation resumable.
        if old_sid:
            isess = interactive_session.get(old_sid)
            if isess is not None and isess.alive:
                # interactive → -p: close() tails the transcript → DB history.
                await isess.close(reason="mode-switch")
            else:
                # -p → interactive: tear down the headless process (keep JSONL).
                try:
                    lyr = get_execution_layer(agent, execution_path=chat.get("execution_path", ""), user_sub=self.user_sub, role=role)
                    if lyr and await lyr.is_session_alive(old_sid):
                        await lyr.close_session(old_sid)
                except Exception:
                    logger.warning(f"switch: headless teardown failed for chat={cid}", exc_info=True)
            if cid == self.chat_id:
                self.session_id = None
        self._detach_pty_viewer()

        # Reload the conversation into the client (the interactive close() just
        # tailed it) so the -p view shows the history; harmless when swapping to
        # the terminal (the messages sit hidden under it).
        if cid.startswith("task-"):
            msgs = task_store.get_chat_messages(cid)
            msgs_has_more = False
        else:
            msgs, msgs_has_more = task_store.get_chat_messages_page(cid, _CHAT_PAGE)
        await self._send({
            "type": "chat_history", "chat_id": cid, "agent": chat.get("agent", ""),
            "messages": msgs, "plans": [],
            "has_more": msgs_has_more,
            "restore": _build_chat_restore(cid),
            "total_cost": chat.get("total_cost") or 0,
            "context_used": chat.get("context_used") or 0,
            "context_max": chat.get("context_max") or 0,
            "execution_path": resolve_execution_path(agent, chat.get("execution_path", "")),
            "execution_mode": new_mode,
            "model": chat.get("model", ""),
            "mode": chat.get("permission_mode", "default"),
        })

        # Re-warm in the target mode — reuses the full warmup machinery; the now
        # dead old session falls through to the resume path → spawns the new mode
        # resuming the JSONL. background=False so warmup_ready (→ UI swap) is sent.
        await self._handle_warmup({
            "agent": agent, "chat_id": cid,
            "permission_mode": chat.get("permission_mode", "default"),
            "model": chat.get("model", ""),
            "execution_path": chat.get("execution_path", ""),
            "execution_mode": new_mode,
            "theme": msg.get("theme", ""),
        }, background=False)


    async def _handle_implement_plan(self, msg: dict):

        plan_path = msg.get("plan_path", "")
        mode = msg.get("mode", "acceptEdits")
        if not plan_path:
            await self._send_error("plan_path required")
            return

        # Close current session (close_session now releases slot + subscription)
        if self.session_id and self.layer:
            await self.layer.close_session(self.session_id)

        # Preserve model from current chat
        chat_rec = task_store.get_chat(self.chat_id) if self.chat_id else None
        chat_model = (chat_rec or {}).get("model", "") or config.get_cli_model(self.agent_name)

        # Create new session via _create_or_resume_session (acquires slot)
        chat_exec_path = (chat_rec or {}).get("execution_path", "")
        new_session_id = str(uuid.uuid4())
        try:
            await self._create_or_resume_session(
                new_session_id, self.agent_name, mode, resume=False,
                model=chat_model, exec_path=chat_exec_path,
                chat_id=self.chat_id,
            )
        except Exception as e:
            await self._send_error(f"Failed to create implementation session: {e}")
            return
        task_store.update_chat(self.chat_id, session_id=self.session_id, permission_mode=mode)

        await self._send({
            "type": "warmup_ready",
            "session_id": self.session_id,
            "chat_id": self.chat_id,
            "mode": mode,
            "model": chat_model,
        })
        logger.info(f"WS dashboard implement plan: new session={self.session_id}, plan={plan_path}, model={chat_model}")
