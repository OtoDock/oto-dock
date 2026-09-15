"""The producer/pump plumbing: starting a stream, streaming through the pump,
entering the pump loop, task pumps and the background monitors.

``ChatStreamMixin`` is one of the mixins ``ws/dashboard_chat.py`` assembles into
``ChatController``; methods run with the connection's full attribute state
and reach the other mixins through ``self``. Nothing here is standalone.
"""

import asyncio
import functools
import json
import logging
import time
from fastapi import WebSocketDisconnect
from storage import database as task_store
from storage.pg import run_db
from core.events import chat_writer
from services.notifications import notification_manager
from core.session.session_state import (
    _chat_streaming_state,
    set_session_mode,
    get_session_mode,
    get_permission_queue,
    resolve_permission,
    resolve_question,
    resolve_location,
    get_subagent_registry,
    clear_session_liveness,
)
from core.events.common_events import CommonEvent, ERROR, QUEUE_TURN, ARTIFACT_TURN, PRODUCER_DONE
from core.execution_layer import ExecutionLayer
from core.session.session_manager import resolve_execution_path
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
from core.session import visibility as _vis
# Imported by ws/dashboard.py AFTER its helpers are defined —
# safe intra-unit circularity (see the class assembly there).
from ws.dashboard import (
    _EXTERNAL_DRIVEN_SOURCES,
)
from ws.dashboard_chat_text import _queued_outgoing

logger = logging.getLogger("claude-proxy")


class ChatStreamMixin:
    """The producer/pump plumbing: starting a stream, streaming through the pump,"""

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
        # A session ABSENT from the pool (idle-reaped, proxy restart) counts as
        # dead here too: is_session_process_dead alone reports False for it
        # ("not in pool = absent"), which used to skip the warm branches and
        # fall through to a silent "Session not found" — a server turn's
        # delegate result then never ran (the 18:56 no-resume bug). The layer
        # predicates keep their semantics; this chokepoint is where "absent"
        # and "dead" must both mean "warm it".
        process_dead = bool(target_layer) and (
            await target_layer.is_session_process_dead(sid)
            or not await target_layer.is_session_alive(sid)
        )
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
            prompt, reseed_notice = await run_db(consume_pending_seed, target_chat_id, prompt)
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
            pending_wakes = await run_db(task_store.claim_pending_delegate_wake, target_chat_id)
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
        # Stop-and-send flags, shared with the pump like the queues above.
        # "fired" debounces to one graceful interrupt per turn boundary;
        # "note" marks that an interrupt actually landed, so the drain
        # prepends the interruption note to the engine prompt. Both reset
        # at each message drain.
        stop_flags: dict = {"fired": False, "note": False}

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

        # Fallback usage scope — on a Shared-only agent the chat row's owner is
        # synthetic, so "agent" is the bucket when no payer is known. When a USER
        # subscription served the session, _record_usage re-attributes the row to
        # that payer (user scope). Use the target chat's agent (resolve from the
        # row only for a non-viewed server turn; the viewed turn's agent is the
        # connection's agent_name).
        # One row read (off-loop) serves the usage scope AND the pump's
        # turn-start broadcast (owner + agent), so the pump never reads the
        # row on the loop. It runs BEFORE the producer task exists: the pump's
        # first suspension must come before any event is queued, so the
        # viewer attaches into an empty stream (live_state snapshot empty,
        # every event forwarded live) — an await between the producer and
        # pump.start() would let the producer fill the queue first.
        _turn_row = await run_db(task_store.get_chat, target_chat_id) or {}
        scope_agent = self.agent_name if is_viewed else (
            _turn_row.get("agent") or self.agent_name
        )
        pump_scope = "agent" if _vis.is_shared_only(scope_agent) else "user"

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
                            # Stop-and-send: the note rides the engine prompt
                            # only — the QUEUE_TURN row above stays raw.
                            outgoing = _queued_outgoing(stop_flags, combined)
                            async for event in target_layer.send_message(sid, outgoing, inject_time=True):
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

        pump = ChatStreamPump(
            chat_id=target_chat_id,
            session_id=sid,
            producer=producer,
            event_queue=event_queue,
            perm_queue=perm_queue,
            implementing_plan=self.implementing_plan if is_viewed else "",
            scope=pump_scope,
            chat_owner=_turn_row.get("user_sub") or "",
            chat_agent=_turn_row.get("agent") or scope_agent or "",
        )
        pump.message_queue = msg_queue  # share the same list with producer
        pump.system_queue = sys_queue   # share system prompt queue with producer
        pump.system_queue_consumer = True  # this producer drains it each turn
        pump.artifact_queue = art_queue  # share artifact-interaction queue too
        pump.stop_and_send = stop_flags  # ownership marker: this producer drains
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
                     # bg residuals after a turn ended — agents AND commands
                     # (a Commands-only residual previously failed this gate
                     # and the badge was lost on reconnect):
                     or live.get("active_agents")
                     or live.get("active_commands")):
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
                        # Mode/plan-status writes ride the chat lane so they
                        # stay ordered with the pump's own plan-mode write.
                        if self.chat_id and plan_fn and action == "reject":
                            chat_writer.submit(
                                self.chat_id,
                                functools.partial(task_store.update_chat_plan_status,
                                                  self.chat_id, plan_fn, "rejected"),
                                label="plan_rejected",
                            )
                            restored_mode = self.pre_plan_mode_holder[0]
                            chat_writer.submit(
                                self.chat_id,
                                functools.partial(task_store.update_chat, self.chat_id,
                                                  permission_mode=restored_mode),
                                label="plan_mode_restore",
                            )
                            await self._send({"type": "mode_changed", "mode": restored_mode})
                        if action == "implement_accept_edits":
                            self.pending_control_requests.append(("set_permission_mode", {"mode": "acceptEdits"}))
                            chat_writer.submit(
                                self.chat_id,
                                functools.partial(task_store.update_chat, self.chat_id,
                                                  permission_mode="acceptEdits"),
                                label="plan_implement_mode",
                            )
                            await self._send({"type": "mode_changed", "mode": "acceptEdits"})
                            pump.queue_message("Please implement the plan now.")
                            pump.implementing_plan = plan_fn
                        elif action == "implement_default":
                            chat_writer.submit(
                                self.chat_id,
                                functools.partial(task_store.update_chat, self.chat_id,
                                                  permission_mode="default"),
                                label="plan_implement_mode",
                            )
                            await self._send({"type": "mode_changed", "mode": "default"})
                            pump.queue_message("Please implement the plan now.")
                            pump.implementing_plan = plan_fn
                    elif cm_type == "chat":
                        text = client_msg.get("text", "")
                        # Same task continue-gate the between-turns/warmup/
                        # permission paths enforce — WITHOUT it a viewer of a
                        # shared-only agent's task chat could steer/queue into a
                        # running task run they're not allowed to continue
                        # (Codex steer injects into the live turn). Applies only
                        # to task-{run} chats; a no-op for regular chats.
                        if text and await self._deny_task_continue(pump.chat_id):
                            text = ""
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
                                await chat_writer.submit(
                                    pump.chat_id,
                                    functools.partial(task_store.add_chat_message,
                                                      pump.chat_id, "user", text,
                                                      author_sub=self.user_sub),
                                    label="steered_row",
                                )
                                await self._send({"type": "steered", "text": text, "chat_id": frame_chat_id})
                            else:
                                idx = pump.queue_message(text)
                                if idx < 0:
                                    await self._send_error(
                                        "Too many queued messages — wait for the "
                                        "current turn to finish.")
                                else:
                                    await self._send({"type": "queued", "index": idx, "text": text, "chat_id": frame_chat_id})
                                    # Stop-and-send (Claude CLI headless): fire a
                                    # graceful-only interrupt so the producer's
                                    # queue drain delivers this message as the
                                    # next turn in seconds, not at turn end.
                                    self._maybe_stop_and_send(pump)
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
                        elif pump._meeting_agent or await run_db(task_store.get_active_meeting_for_chat, a_chat):
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
                        elif pump._meeting_agent or await run_db(task_store.get_active_meeting_for_chat, ap_chat):
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
                            _abort_cid = self.chat_id

                            def _abort_job(_g=graceful) -> dict | None:
                                # Flag write then the row read, one lane job.
                                task_store.update_chat(_abort_cid,
                                                       last_turn_aborted=True,
                                                       last_abort_graceful=_g)
                                return task_store.get_chat(_abort_cid)

                            _abort_chat = await chat_writer.submit(
                                _abort_cid, _abort_job, label="abort_flags",
                            )
                            # A hard CLI Stop kills the whole process group —
                            # any bg agents/commands died with it; clear their
                            # badges. Graceful keeps the process (and its bg
                            # work) alive; Codex keeps its daemon either way.
                            if self.session_id and not graceful:
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
                        await self._pong()
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
                pump = await run_db(self._find_task_pump)
            if not pump or pump.is_done:
                # Wait briefly for next pump: task chats (multi-turn) OR meetings
                is_meeting = bool(await run_db(task_store.get_active_meeting_for_chat, self.chat_id))
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
                or await run_db(task_store.get_active_meeting_for_chat, self.chat_id)
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
                    chat_writer.submit(
                        self.chat_id,
                        functools.partial(task_store.update_chat_plan_status,
                                          self.chat_id, pump.implementing_plan, "implemented"),
                        label="plan_implemented",
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
