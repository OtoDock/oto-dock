"""User sends: the chat turn itself, stop-and-send, delivery into a live
interactive session, artifact interactions, app actions and read receipts.

``ChatSendMixin`` is one of the mixins ``ws/dashboard_chat.py`` assembles into
``ChatController``; methods run with the connection's full attribute state
and reach the other mixins through ``self``. Nothing here is standalone.
"""

import asyncio
import functools
import json
import logging
import config
from core import placement
from storage import database as task_store
from storage.pg import run_db
from core.events import chat_writer
from services.notifications import notification_manager
from core.session.session_state import (
    get_session_mode,
)
from core.events.common_events import TurnInput
from core.events.stream_pump import (
    _active_pumps,
)
from core.session import warmup_registry, visibility as _vis, interactive_session
# Imported by ws/dashboard.py AFTER its helpers are defined —
# safe intra-unit circularity (see the class assembly there).
from ws.dashboard import (
    _role_and_layer,
)
from ws.dashboard_chat_text import _REVIVAL_WAIT_S
from ws import wire_events as wire

logger = logging.getLogger("claude-proxy")


class ChatSendMixin:
    """User sends: the chat turn itself, stop-and-send, delivery into a live"""

    async def _pool_cap_gate(self) -> str:
        """The sender's own-pool subscription cap on this chat's engine
        (``services.billing.pool_caps``). Returns ``blocked`` (a
        ``limit_reached`` went out, the turn stops), ``continue`` (a hit
        under ``continue`` with a key to continue on: the turn runs and the
        session is moved off its OAuth account) or ``ok``; a warning and a
        continued hit both send ``limit_warning`` with the ``pool`` reading."""
        from core.session.session_manager import resolve_execution_path
        from services.billing import pool_caps
        from services.engines import subscription_pool
        row = await run_db(task_store.get_chat, self.chat_id) if self.chat_id else None
        layer_path = await run_db(
            resolve_execution_path, self.agent_name, (row or {}).get("execution_path", ""))
        cap = await run_db(pool_caps.evaluate, "user", self.user_sub, layer_path)
        if cap.allowed:
            if cap.warning:
                await self._send({"type": wire.LIMIT_WARNING, "pool": cap.to_public()})
            return "ok"
        if cap.on_reached == "continue" and await run_db(
            subscription_pool.cap_continue_available, layer_path, self.user_sub,
        ):
            await self._send({"type": wire.LIMIT_WARNING, "pool": cap.to_public()})
            return "continue"
        await self._send({"type": wire.LIMIT_REACHED, "pool": cap.to_public()})
        return "blocked"

    async def _recycle_session(self, why: str) -> None:
        """Close the chat's live session and re-warm it synchronously, so the
        turn about to run binds a fresh acquisition. close_session releases
        the slot and the subscription; the JSONL stays, so the re-warm
        resumes the conversation (one respawn and its prompt cache)."""
        logger.info(
            f"WS dashboard _handle_chat: {why} — recycling session "
            f"{self.session_id} for chat={self.chat_id}"
        )
        try:
            await self.layer.close_session(self.session_id)
        except Exception:
            logger.warning(
                f"session recycle: close_session failed for {self.session_id}",
                exc_info=True,
            )
        self.session_id = None
        await self._handle_warmup({
            "agent": self.agent_name,
            "chat_id": self.chat_id,
            "permission_mode": "default",
        }, background=False)

    async def _recycle_if_on_own_oauth(self) -> None:
        """Under a pool cap's ``continue``: a warm session still bound to one
        of the sender's own OAuth accounts is recycled so the re-warm takes
        a key (the pool excludes the OAuth accounts at spawn). Never
        mid-turn (a queued turn rides its session) and never an interactive
        terminal (a PTY holds live TUI state)."""
        from services.engines import subscription_pool
        from storage.billing import subscription_store
        pump = _active_pumps.get(self.chat_id)
        if pump and not pump.is_done:
            return
        if interactive_session.get(self.session_id) is not None:
            return
        bound = await run_db(subscription_pool.get_session_subscription, self.session_id)
        row = await run_db(subscription_store.get_subscription, bound) if bound else None
        if not row or row.get("auth_type") != "oauth" or row.get("owner_sub") != self.user_sub:
            return
        await self._recycle_session("pool cap reached, session on an own OAuth account")

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

        # A bound socket runs the turn on its bound chat, so a frame naming
        # another chat is refused: the gates below judge the chat that runs,
        # and a message meant for one chat never lands in another.
        if self.chat_id and self.agent_name and msg_chat_id and msg_chat_id != self.chat_id:
            await self._send_error("This message is for a chat this connection "
                                   "is not on. Reopen the chat and send it again.")
            await self._send({"type": wire.DONE, "chat_id": msg_chat_id})
            return

        # Task continue-gate (security boundary): a turn must never be driven
        # on a task this user can't continue — agent-scoped → editor+,
        # user-scoped → creator/admin. Checks the chat being driven (the
        # message's chat_id, or the WS's current chat_id on the normal path).
        if await self._deny_task_continue(msg_chat_id or self.chat_id):
            await self._send({"type": wire.DONE, "chat_id": msg_chat_id or self.chat_id or ""})
            return

        # Self-heal: if WS state is missing (post-reconnect race where `chat`
        # arrives before `resume_chat`, or `resume_chat` was never sent — common
        # on Android after a screen off/on cycle if the visibility handler missed
        # the dead-WS health check), restore agent_name/chat_id/layer from the
        # DB using the chat_id the client sent. Without this, the auto-warmup
        # below is skipped and the user sees "No session — send warmup first".
        if msg_chat_id and (not self.chat_id or not self.agent_name):
            chat = await run_db(task_store.get_chat, msg_chat_id)
            if chat:
                # The rule resume_chat binds by (the chat's owner decides,
                # never the agent's current mode). Imported here: the agents
                # API package imports this module's assembly.
                from api.agents.agents import can_open_chat
                allowed = await run_db(can_open_chat, self._viewer_context(), chat)
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
                    # Pinned target for resume affinity — the recovered layer
                    # drives liveness/resume checks for this chat's session
                    # (see _do_warmup). Role + layer in ONE executor job.
                    _, self.layer = await run_db(
                        _role_and_layer, self.user_sub, self.agent_name, chat,
                        fallback_user=self.user,
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
                        # Pinned target so a REMOTE session's recovery stays off
                        # the local ceiling; the owner is recorded only for a
                        # connection that drives the chat, a re-attach is never
                        # capped.
                        if await acquire_chat_slot(
                                pump.session_id,
                                target=chat.get("execution_target") or placement.LOCAL,
                                execution_path=chat.get("execution_path"),
                                user_sub=None if self._view_only else self.user_sub,
                                per_user_cap=False):
                            self.session_id = pump.session_id
                            await self._send({
                                "type": wire.WARMUP_READY,
                                "session_id": self.session_id,
                                "chat_id": self.chat_id,
                                "mode": get_session_mode(self.session_id) or chat.get("permission_mode", "default"),
                                "model": chat.get("model", ""),
                                "execution_path": chat.get("execution_path") or "",
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
                await self._send({"type": wire.LIMIT_REACHED, **limit_status["periods"]})
                return
            if limit_status["warning"]:
                await self._send({"type": wire.LIMIT_WARNING, **limit_status["periods"]})
        except Exception:
            pass  # Don't block messages if limit check fails

        # ── Subscription pool cap (the sender's own accounts on this chat's
        # engine): same shape as the dollar gate above. A hit under
        # ``continue`` lets the turn through on a key and recycles a warm
        # session off its OAuth account further down.
        cap_verdict = "ok"
        try:
            cap_verdict = await self._pool_cap_gate()
        except Exception:
            logger.debug("pool cap gate failed, turn proceeds", exc_info=True)
        if cap_verdict == "blocked":
            return

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
                # The headless liveness check above never sees PTY sessions
                # (they live in interactive_session, not the layer's registry):
                # a live interactive revival must take this send, or the warmup
                # below spawns a headless TWIN on the same session id whose
                # stale sibling's eventual teardown wipes the survivor's
                # permission/secret state (incident 2026-08-06, chat 9f56bf65).
                if await self._deliver_chat_to_live_interactive(msg):
                    return
                # Single-flight: a warmup already in flight for this chat (the
                # chat-open revival racing this send) owns the respawn — await
                # it bounded, then re-check BOTH registries. Only a still-dead
                # session with none in flight synthesizes a warmup below.
                inflight = warmup_registry.get(self.chat_id)
                if inflight is not None:
                    logger.info(
                        f"WS dashboard _handle_chat: warmup in flight for "
                        f"chat={self.chat_id} — awaiting it instead of re-warming"
                    )
                    try:
                        await asyncio.wait_for(
                            inflight.completed.wait(), timeout=_REVIVAL_WAIT_S)
                    except asyncio.TimeoutError:
                        logger.warning(
                            f"WS dashboard _handle_chat: in-flight warmup for "
                            f"chat={self.chat_id} still running after "
                            f"{_REVIVAL_WAIT_S:.0f}s — falling back to auto-warm"
                        )
                    if await self._deliver_chat_to_live_interactive(msg):
                        return
                    # The finished warmup may have bound a NEW session id to the
                    # chat row — adopt it (+ its layer) before re-checking.
                    _cur = await run_db(task_store.get_chat, self.chat_id) or {}
                    _cur_sid = _cur.get("session_id") or ""
                    if _cur_sid and _cur_sid != self.session_id:
                        self.session_id = _cur_sid
                        _, self.layer = await run_db(
                            _role_and_layer, self.user_sub, self.agent_name, _cur,
                            fallback_user=self.user,
                        )
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
                #
                # The payer check alone is not enough: when the platform or
                # agent pool paid, ``get_session_payer_sub`` returns "" and the
                # handover would silently keep the previous sender's JWT,
                # ``OTO_*`` env and platform role — an identity carry-over, not
                # just a billing one. So ALSO compare the session's warmed
                # identity, and fail CLOSED when it can't be proven: a needless
                # recycle costs one respawn + JSONL resume, a skipped one lets
                # this turn act as the previous sender.
                from services.engines import subscription_pool
                from core.session import session_state
                # Interactive sessions never recycle implicitly: a PTY holds
                # live TUI state, and this branch is reachable WITHOUT the other
                # user typing anything (an idle artifact-backchannel send or a
                # app action routes here), so recycling would kill a
                # colleague's terminal mid-work. Refuse and let them take it over
                # deliberately.
                _isess = interactive_session.get(self.session_id)
                if _isess is not None and not _isess.may_drive(self.user_sub):
                    await self._send_pty_read_only(_isess)
                    return
                pump = _active_pumps.get(self.chat_id)
                turn_in_flight = bool(pump and not pump.is_done)
                payer = subscription_pool.get_session_payer_sub(self.session_id)
                warmed = session_state.get_session_security(self.session_id)
                identity_stale = (
                    warmed is None
                    or (warmed.username or "") != (self.user.get("username") or "")
                )
                if not turn_in_flight and (
                    (payer and payer != self.user_sub) or identity_stale
                ):
                    await self._recycle_session(
                        "shared-only chat changed sender, binding the new "
                        "sender's subscription and identity")

        if cap_verdict == "continue" and self.session_id:
            await self._recycle_if_on_own_oauth()

        if not self.session_id:
            await self._send_error("No session — send warmup first")
            return

        # The turn's input — the same derivation a busy send takes in the
        # dispatcher's steer/queue branch (`_prepare_turn_input`).
        turn_input = await self._prepare_turn_input(text, images, files)
        if turn_input is None:
            # The refusal went out as an error frame; the client armed its
            # streaming state on the send, so end the turn it never got.
            await self._send({"type": wire.DONE, "chat_id": self.chat_id or ""})
            return
        is_agent_scoped = _vis.is_shared_only(self.agent_name)
        cli_text, attached_images = turn_input.cli_text, turn_input.images

        # Save original user text to DB (without image/file paths injection)
        event_meta = turn_input.event_meta
        event_data = json.dumps(event_meta) if event_meta else ""

        # ONE chat-lane job for the send-time row work, ordered against the
        # pump's rows and against any other sender of the same chat:
        #   * Shared-only: the previous author, read BEFORE this message
        #     persists (afterwards the sender IS the last author) — drives
        #     the speaker-transition note injected below.
        #   * The user row.
        #   * The stable deterministic title at first-message time — ONE
        #     conditional write, so a rename that already landed wins. No
        #     LLM, no post-turn rename churn.
        #   * The cancelled-turn context: a hard abort (killpg / stream
        #     cancel) loses the partial turn engine-side, so it is read back
        #     from our DB and prepended; a GRACEFUL abort kept it in the
        #     engine's own history — injecting would duplicate it. Skipped
        #     while a history seed is pending (the digest injected at
        #     _start_new_stream already contains the aborted turn).
        _cid = self.chat_id
        _sender = self.user_sub
        _persist_row = not server_kick and not artifact_framed
        _read_prev_author = is_agent_scoped and bool(_cid) and not server_kick
        _title = self._deterministic_title(text) if (_cid and not artifact_framed) else ""

        _focus_conn = getattr(self, "notify_connection_id", "") or None
        _focus_user = self.user_sub
        # A server kick is the warmup's own first prompt: the line was
        # captured at send time (the overlay closes on send and the kick runs
        # seconds later), so the kick carries it instead of a live read.
        _focus_captured = msg.get("_focus_line") if server_kick else None

        def _send_job() -> dict:
            out = {"prev_author": "", "title_set": False, "rec": None, "cancelled": "",
                   "focus": ""}
            # The app on THIS sender's screen, if any (APPS.md "Live
            # apps"). Best effort: a focus failure must never hold a send.
            if _focus_captured is not None:
                out["focus"] = str(_focus_captured or "")
            elif _focus_conn:
                try:
                    from services.apps.focus_context import focus_line
                    out["focus"] = focus_line(_focus_user, connection_id=_focus_conn)
                except Exception:
                    logger.debug("focus line unavailable for chat %s", _cid, exc_info=True)
            if _read_prev_author:
                out["prev_author"] = task_store.get_last_user_message_author(_cid)
            if _persist_row:
                task_store.add_chat_message(_cid, "user", text, event_data=event_data,
                                            author_sub=_sender)
            if not _cid:
                return out
            rec = task_store.get_chat(_cid)
            out["rec"] = rec
            if rec and _title and task_store.set_chat_title_if_unset(_cid, _title):
                out["title_set"] = True
            if rec and rec.get("last_turn_aborted") and not rec.get("pending_history_seed"):
                graceful_abort = bool(rec.get("last_abort_graceful"))
                task_store.update_chat(_cid, last_turn_aborted=False,
                                       last_abort_graceful=False)
                if not graceful_abort:
                    out["cancelled"] = self._build_cancelled_context(_cid)
            return out

        sent = await chat_writer.submit(_cid, _send_job, label="chat_send")
        prev_author = sent["prev_author"]
        chat_rec = sent["rec"]
        if sent["title_set"]:
            # BOTH sends, deliberately: the broadcast reaches every other
            # viewer's sidebar + Active-now widget now, but the SENDING
            # socket's notify queue drains only between turns — the direct
            # send is what titles the sending tab for the whole first
            # turn. The client's title_updated patch is idempotent.
            await self._send({"type": wire.TITLE_UPDATED, "chat_id": self.chat_id, "title": _title})
            try:
                notification_manager.broadcast_chat_title(
                    (chat_rec or {}).get("user_sub") or "", self.chat_id, _title,
                    agent=(chat_rec or {}).get("agent") or "",
                )
            except Exception:
                logger.debug("first-message title broadcast failed for %s",
                             self.chat_id, exc_info=True)
        if sent["cancelled"]:
            cli_text = sent["cancelled"] + "\n\n" + cli_text
            logger.info(f"WS dashboard: injected cancelled turn context ({len(sent['cancelled'])} chars) for chat={self.chat_id}")
        # Prompt-side only, like the notes around it: the stored row is the
        # raw text.
        if sent.get("focus"):
            cli_text = sent["focus"] + "\n\n" + cli_text

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
                batch = TurnInput.combine(self.message_queue)
                self.message_queue.clear()
                await self._send({"type": wire.QUEUE_SENT, "text": batch.text,
                                  **batch.frame_fields()})
                # Lane-ordered after the finished turn's rows (drained at its
                # pump end) and before the next pump's.
                _batch_meta = batch.event_meta
                await chat_writer.submit(
                    self.chat_id,
                    functools.partial(task_store.add_chat_message, self.chat_id,
                                      "user", batch.text,
                                      event_data=json.dumps(_batch_meta) if _batch_meta else "",
                                      author_sub=self.user_sub),
                    label="queue_drain",
                )
                pump = await self._start_new_stream(
                    batch.cli_text,
                    target_session_id=self.session_id,
                    target_chat_id=self.chat_id,
                    target_layer=self.layer,
                    images=batch.images or None,
                )
                if pump:
                    await self._enter_pump_loop()
                else:
                    await self._send({"type": wire.DONE, "chat_id": self.chat_id or ""})
                    break
            # Leftover backchannel interactions (queued via the between-turns
            # dispatcher during a transient streaming state, so never adopted
            # by a producer) — deliver now rather than waiting for a future
            # user send. The rows persist here; the frames render the chips.
            while self.artifact_queue and self.session_id:
                from ws import artifact_interactions as _ai
                batch = list(self.artifact_queue)
                self.artifact_queue.clear()
                _rows = [(_ai.event_type(it), _ai.event_row_json(it)) for it in batch]
                _bcid = self.chat_id

                def _batch_job(_r=_rows) -> None:
                    for _etype, _edata in _r:
                        task_store.add_chat_message(
                            _bcid, "event", "", event_type=_etype, event_data=_edata,
                        )

                await chat_writer.submit(_bcid, _batch_job, label="artifact_batch")
                for it in batch:
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
                    await self._send({"type": wire.DONE, "chat_id": self.chat_id or ""})
                    break
        else:
            # Pump creation failed (dead session, error) — reset frontend streaming state
            await self._send({"type": wire.DONE, "chat_id": self.chat_id or ""})

    def _maybe_stop_and_send(self, pump) -> None:
        """Typed message queued onto a busy turn → graceful stop-and-send.

        Fires only on pumps OWNED by ``_start_new_stream`` (``stop_and_send``
        flags present — its producer drains ``message_queue``); foreign
        producers (task runs, meetings, duplex turns, delegate-result
        echoes, recovery adopts) never drain typed messages, so interrupting
        them would destroy the turn for nothing — they keep queue-only.
        Steered engines (Codex) never reach the queue branch. The layer call
        is graceful-ONLY (`interrupt_for_queued`): no killpg, no watchdog —
        background bash/subagents survive, and a turn that ignores the
        interrupt degrades to today's deliver-at-turn-end.
        """
        flags = getattr(pump, "stop_and_send", None)
        if flags is None or flags["fired"] or self.layer is None:
            return
        flags["fired"] = True
        asyncio.create_task(self._fire_stop_and_send(pump, flags))

    async def _fire_stop_and_send(self, pump, flags: dict) -> None:
        # Pre-flight re-checks (the ack await above yielded the loop): the
        # producer may have claimed the queue already (the interrupt would
        # land on the DRAIN turn delivering this very message), the user may
        # have cancelled the queued message, or the pump may have been
        # superseded (never interrupt the successor's turn). Un-latch the
        # debounce on every no-fire path so a later typed message can fire.
        if not pump.message_queue or _active_pumps.get(pump.chat_id) is not pump:
            flags["fired"] = False
            return
        try:
            # The pump's session, never the connection's — an attach viewer
            # may hold a different session id than the streaming turn.
            if await self.layer.interrupt_for_queued(pump.session_id):
                flags["note"] = True
                # The auto-deny released the hook waiters; clear the pump's
                # permission slot too, else the drain turn's next permission
                # buffers invisibly behind the dead card.
                pump.clear_permission_state()
                logger.info(
                    f"stop-and-send: graceful interrupt fired for "
                    f"chat={pump.chat_id[:8]}"
                )
            else:
                flags["fired"] = False
        except Exception:
            flags["fired"] = False
            logger.warning("stop-and-send: interrupt attempt failed",
                           exc_info=True)

    async def _deliver_chat_to_live_interactive(self, msg: dict) -> bool:
        """Deliver a composer ``chat`` frame to a LIVE interactive session on
        the viewed chat, if one exists. Returns True when the send was handled
        (typed into the PTY, held by its question-parked queue, or refused
        read-only) — the caller must NOT warm. Delivery mirrors the server-side
        first-prompt submit in ``_spawn_tail`` (persist the raw row, stamp the
        time prelude, note the stamped bytes so the tailer's duplicate-skip
        matches the journaled user line byte-for-byte)."""
        if not self.chat_id:
            return False
        isess = interactive_session.get(self.session_id) if self.session_id else None
        if isess is None or not isess.alive or isess.chat_id != self.chat_id:
            # Session-id drift: a revival may have re-warmed under a new id.
            isess = interactive_session.find_live_for_chat(self.chat_id)
        if isess is None or not isess.alive:
            return False
        if not isess.may_drive(self.user_sub):
            await self._send_pty_read_only(isess)
            return True

        text = msg.get("text", "")
        cli_text = text
        image_meta: list = []
        valid_files: list = []
        if msg.get("images") or msg.get("files"):
            # Same pipeline as the pty_attachments frame: save + push the
            # attachments, build the prompt with sandbox-virtual paths. A
            # refusal (a viewer's photo into a Shared-only chat) is handled
            # here: the error frame answers the send, nothing is typed.
            from ws.dashboard_chat_support import AttachmentsRefused
            try:
                cli_text, _imgs, image_meta, valid_files = await self._process_attachments(
                    text, msg.get("images", []), msg.get("files", []),
                    agent=self.agent_name, agent_dir=config.get_agent_dir(self.agent_name),
                    is_agent_scoped=_vis.is_shared_only(self.agent_name),
                    username=self.user.get("username") or "",
                    is_direct_llm=False,
                )
            except AttachmentsRefused as e:
                await self._send_error(str(e))
                return True

        # Adopt the live session + tell the client it is interactive so it
        # attaches the terminal (same frame shape as the resume re-attach) —
        # the client sent `chat` precisely because it believed the session
        # dead.
        from core.concurrency import acquire_chat_slot
        await acquire_chat_slot(isess.session_id, target=isess.target,
                                user_sub=None if self._view_only else self.user_sub,
                                per_user_cap=False)
        self.session_id = isess.session_id
        chat_rec = await run_db(task_store.get_chat, self.chat_id) or {}
        await self._send({
            "type": wire.WARMUP_READY,
            "session_id": self.session_id,
            "chat_id": self.chat_id,
            "mode": get_session_mode(self.session_id) or chat_rec.get("permission_mode", "default"),
            "model": chat_rec.get("model", "") or config.get_cli_model(self.agent_name),
            "execution_path": chat_rec.get("execution_path", ""),
            "interactive": True,
            "turn_open": isess.turn_open,
        })

        # A _server_kick's prompt was persisted at _do_warmup send-time; an
        # artifact-framed turn's distinct row was persisted by its handler.
        if not msg.get("_server_kick") and not msg.get("_artifact_framed"):
            event_meta = {}
            if image_meta:
                event_meta["images"] = image_meta
            if valid_files:
                event_meta["files"] = valid_files
            # Awaited: the row must exist before note_sent_prompt below, or
            # the tailer's duplicate-skip and the row race.
            await chat_writer.submit(
                self.chat_id,
                functools.partial(
                    task_store.add_chat_message, self.chat_id, "user", text,
                    event_data=json.dumps(event_meta) if event_meta else "",
                    author_sub=self.user_sub,
                ),
                label="live_send",
            )
        # The focus line is a second prelude after the time stamp on the
        # interactive rail; the tailer keeps both and the prelude matchers
        # strip both (APPS.md "Live apps").
        focus = ""
        try:
            from services.apps.focus_context import focus_line
            focus = await run_db(
                focus_line, self.user_sub,
                connection_id=getattr(self, "notify_connection_id", "") or None,
            )
        except Exception:
            logger.debug("focus line unavailable for chat %s", self.chat_id, exc_info=True)
        stamped = self._interactive_prelude(cli_text, session_id=self.session_id, focus_line=focus)
        from core.session.transcript_tool_events import note_sent_prompt
        note_sent_prompt(self.chat_id, stamped)
        # Bracketed paste + CR (the composer's own framing): the question-
        # parked hold in deliver_dashboard_input strips exactly this shape.
        isess.deliver_dashboard_input(
            ("\x1b[200~" + stamped + "\x1b[201~\r").encode("utf-8"),
            composer=True, sender_sub=self.user_sub,
        )
        logger.info(
            f"WS dashboard _handle_chat: delivered composer send to live "
            f"interactive session={self.session_id[:8]}, chat={self.chat_id[:8]}"
        )
        return True

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
            frame: dict = {"type": wire.ARTIFACT_ACK, "token": token, "status": status}
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
        if await run_db(task_store.get_active_meeting_for_chat, chat_id):
            return await ack("unavailable", "meeting in progress")
        if refusal := await self._deny_task_continue(chat_id):
            return await ack("denied", refusal)
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

        # Idle: persist the distinct row (awaited — the framed turn below
        # skips its own user row BECAUSE this one exists), then run the framed
        # turn through the normal chat path (auto-warmup, limits,
        # cancelled-context — with the _artifact_framed downgrades). Ack
        # BEFORE the turn so the artifact's button state settles while the
        # agent works.
        await chat_writer.submit(
            chat_id,
            functools.partial(
                task_store.add_chat_message, chat_id, "event", "",
                event_type=wire.ARTIFACT_INTERACTION,
                event_data=_ai.event_row_json(interaction),
            ),
            label="artifact_row",
        )
        await ack("sent")
        await self._handle_chat({
            "text": _ai.frame_text([interaction]),
            "chat_id": chat_id,
            "_artifact_framed": True,
        })

    async def _handle_app_action(self, msg: dict):
        """Between-turns entry for a pinned app send_prompt action (the
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
            frame: dict = {"type": wire.APP_ACTION_ACK, "app_id": app_id,
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
        if await run_db(task_store.get_active_meeting_for_chat, chat_id):
            return await ack("unavailable", "meeting in progress")
        if refusal := await self._deny_task_continue(chat_id):
            return await ack("denied", refusal)
        if not _ai.check_rate(chat_id, f"app:{app_id}"):
            return await ack("denied", "rate limited")

        # A chat whose FIRST content is an app action (front-page button →
        # fresh chat) never gets the send-time title (the framed turn skips
        # it by design) — name it from the action itself, matching the chip:
        # "Infra Dashboard — Refresh data". One conditional write on the
        # chat's lane (a title that already landed wins). The LLM title
        # upgrade may still improve it after the first completed turn.
        _title = self._deterministic_title(
            f"{interaction['title'] or interaction['slug']} — "
            f"{interaction['label'] or interaction['action_id']}"
        )
        if await chat_writer.submit(
            chat_id, functools.partial(task_store.set_chat_title_if_unset, chat_id, _title),
            label="app_title",
        ):
            await self._send({"type": wire.TITLE_UPDATED, "chat_id": chat_id,
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

        await chat_writer.submit(
            chat_id,
            functools.partial(
                task_store.add_chat_message, chat_id, "event", "",
                event_type=wire.APP_ACTION, event_data=_ai.event_row_json(interaction),
            ),
            label="app_action_row",
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
        _uid, _viewer = self.user_sub, self._viewer_context()
        # Imported here: the agents API package imports this module's assembly.
        from api.agents.agents import can_open_chat

        def _read_job() -> tuple[str, str] | None:
            # Access gate (the resume gate's rule) + the read mark in ONE
            # executor job.
            chat = task_store.get_chat(cid)
            if not chat or not can_open_chat(_viewer, chat):
                return None
            owner = chat.get("user_sub", "")
            chat_agent = chat.get("agent", "")
            task_store.mark_chat_read(cid, _vis.chat_history_owner(chat_agent, _uid))
            return owner, chat_agent

        res = await run_db(_read_job)
        if res is None:
            return
        owner, chat_agent = res
        notification_manager.broadcast_chat_read(owner, cid, agent=chat_agent)
