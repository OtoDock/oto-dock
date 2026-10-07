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
from core.events import chat_writer, input_queue
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
from ws.dashboard_chat_text import (
    HEAL_ANSWERED, HEAL_READY, HEAL_RECONNECTING, RECONNECTING_NOT_SENT, TurnDeferred,
    _REVIVAL_WAIT_S, grace_layer,
)
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

    async def _heal_viewed_session(self, msg: dict) -> str:
        """The viewed chat's session, ready for a turn: warmed when there is
        none, re-warmed when it died (a live interactive revival takes the
        send instead), recycled when a Shared-only chat changed sender, given
        the mode or model picked while it was away. ``HEAL_ANSWERED`` when the
        send was answered here (typed into a live terminal or refused
        read-only) and the caller stops; ``HEAL_RECONNECTING`` when its
        machine is still reconnecting after the wait (nothing was touched,
        the send waits); else ``HEAL_READY``. ``msg`` is the composer frame a
        live terminal would take."""
        # Auto-warmup if we have agent but no session. background=False: this
        # turn needs the session synchronously (the caller reads session_id
        # right after), so wait for the spawn rather than backgrounding it.
        # A reconnect already waited out (that warmup's reuse branch, or the
        # warmup whose inline kick this send is) is not waited for again.
        warmed_here = bool(msg.get("_reconnect_waited"))
        if not self.session_id and self.agent_name and self.chat_id:
            warmed_here = True
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
                    return HEAL_ANSWERED
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
                        return HEAL_ANSWERED
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
            if not alive and self.layer:
                # A machine in its reconnect grace is coming back: the dead
                # path below would drop the live session.
                holder = grace_layer(self.session_id, self.layer)
                if not (warmed_here and holder.is_session_grace_held(self.session_id)):
                    alive = await holder.wait_session_reconnect(self.session_id)
                if not alive and holder.is_session_grace_held(self.session_id):
                    logger.info(f"WS dashboard _handle_chat: session {self.session_id} "
                                f"is still reconnecting, chat={self.chat_id} — the send waits")
                    return HEAL_RECONNECTING
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
                # Interactive sessions never recycle implicitly: a PTY holds
                # live TUI state, and this branch is reachable WITHOUT the other
                # user typing anything (an idle artifact-backchannel send or a
                # app action routes here), so recycling would kill a
                # colleague's terminal mid-work. Refuse and let them take it over
                # deliberately.
                _isess = interactive_session.get(self.session_id)
                if _isess is not None and not _isess.may_drive(self.user_sub):
                    await self._send_pty_read_only(_isess)
                    return HEAL_ANSWERED
                pump = _active_pumps.get(self.chat_id)
                turn_in_flight = bool(pump and not pump.is_done)
                if not turn_in_flight and not self._session_serves_sender(self.session_id):
                    await self._recycle_session(
                        "shared-only chat changed sender, binding the new "
                        "sender's subscription and identity")
        return HEAL_READY

    async def _send_reconnecting_line(self, cid: str) -> None:
        await self._send({"type": wire.SYSTEM, "subtype": wire.SUBTYPE_MACHINE_RECONNECTING,
                          "message": RECONNECTING_NOT_SENT, "chat_id": cid})

    async def _queue_until_reconnect(self, msg: dict, q) -> None:
        """A send whose chat's machine is still reconnecting waits in the
        chat's queue, its chip saying why, and goes out at the reconnect (the
        reconnect pass arms the queue). The ``queued`` frame reaches this
        socket before ``done``: the client turns its optimistic bubble into
        the chip only while its send is pending. A turn whose row is already
        written (a server kick, an interaction) cannot wait there: it gets
        the not-sent line instead."""
        cid = self.chat_id or ""
        if q is None:
            await self._send_reconnecting_line(cid)
        else:
            item = await self._prepare_turn_input(msg.get("text", ""), msg.get("images", []),
                                                  msg.get("files", []))
            if item is not None:
                queue_id = str(msg.get("queue_id") or "")
                if queue_id in q.accepted:
                    q.accepted.remove(queue_id)   # taken at the claim, never by a turn
                await self._queue_for_next_turn(cid, item, queue_id or None,
                                                waiting=input_queue.WAITING_RECONNECT)
                await self._drain_live_queue()
        await self._send({"type": wire.DONE, "chat_id": cid})

    def _session_serves_sender(self, sid: str) -> bool:
        """Whether a warm session of a Shared-only agent runs as this
        connection's person: its payer and the identity it was warmed with
        (JWT, ``OTO_*`` env, platform role). Fails closed: a session whose
        identity cannot be proven serves nobody, since a needless recycle
        costs one respawn and a skipped one lets the turn act as the
        previous sender."""
        from services.engines import subscription_pool
        from core.session import session_state
        payer = subscription_pool.get_session_payer_sub(sid)
        warmed = session_state.get_session_security(sid)
        if warmed is None or (warmed.username or "") != (self.user.get("username") or ""):
            return False
        return not (payer and payer != self.user_sub)

    async def _sender_refusal(self, cid: str, chat: dict | None = None) -> str:
        """Why this person may not start a turn on ``cid`` now, by the gates
        an idle send meets before its turn: their platform-auth budget and
        their own pool's cap on the chat's engine. "" when they may (a gate
        that fails to answer never holds a turn, as at send time)."""
        try:
            from services.billing import usage_service
            limit = await asyncio.to_thread(usage_service.check_user_limit,
                                            self.user_sub, self.user_role)
            if not limit["allowed"]:
                return "the sender's usage limit is reached"
        except Exception:
            pass
        try:
            from core.session.session_manager import resolve_execution_path
            from services.billing import pool_caps
            from services.engines import subscription_pool
            row = chat or await run_db(task_store.get_chat, cid) or {}
            layer_path = await run_db(resolve_execution_path, row.get("agent") or self.agent_name,
                                      row.get("execution_path", ""))
            cap = await run_db(pool_caps.evaluate, "user", self.user_sub, layer_path)
            if not cap.allowed and not (cap.on_reached == "continue" and await run_db(
                    subscription_pool.cap_continue_available, layer_path, self.user_sub)):
                return "the sender's subscription cap is reached"
        except Exception:
            logger.debug("pool cap gate failed for a queued turn", exc_info=True)
        return ""

    async def _prepared_prompt(self, cid: str, cli_text: str, *,
                               new_text: str) -> tuple[str, dict | None]:
        """The engine prompt of a queued batch whose rows are not written
        yet, prepared as ``_handle_chat``'s send job prepares an idle send's:
        the cancelled turn's context after a hard abort or a lost process
        (none when the batch repeats it), and on a Shared-only chat the note
        that names the sender when the last message was someone else's. One
        lane job. Also returns the abort flags it consumed (None when there
        were none): a turn that then fails to start puts them back
        (``_restore_abort_flags``)."""
        sender = self.user_sub

        def _job() -> dict:
            out = {"prev_author": "", "cancelled": "", "consumed": None}
            rec = task_store.get_chat(cid)
            if not rec:
                return out
            if _vis.is_shared_only(rec.get("agent") or ""):
                out["prev_author"] = task_store.get_last_user_message_author(cid)
            if rec.get("last_turn_aborted") and not rec.get("pending_history_seed"):
                graceful = bool(rec.get("last_abort_graceful"))
                task_store.update_chat(cid, last_turn_aborted=False, last_abort_graceful=False)
                out["consumed"] = {"last_turn_aborted": True, "last_abort_graceful": graceful}
                if not graceful:
                    out["cancelled"] = self._build_cancelled_context(cid, new_text=new_text)
            return out

        prep = await chat_writer.submit(cid, _job, label="queued_prompt")
        if prep["cancelled"]:
            cli_text = prep["cancelled"] + "\n\n" + cli_text
        if prep["prev_author"] and prep["prev_author"] != sender:
            name = self.user.get("name") or self.user.get("username") or "another teammate"
            cli_text = (f"[Shared chat: this message is from {name}; earlier user "
                        f"messages may be from other teammates.]\n\n" + cli_text)
        return cli_text, prep["consumed"]

    @staticmethod
    def _restore_abort_flags(cid: str, consumed: dict | None) -> None:
        """The abort flags ``_prepared_prompt`` consumed, back on the chat
        for the turn that will carry the batch (this one did not start)."""
        if consumed:
            chat_writer.submit(cid, functools.partial(task_store.update_chat, cid, **consumed),
                               label="abort_flags_restore")

    @staticmethod
    async def _push_batch_attachments(agent: str, batch: TurnInput) -> None:
        """The batch's photos and files, pushed again to the agent's machines
        before its turn: one uploaded while the chat's machine was away never
        reached it, and the reconnect takes the session back without the
        reconcile a session start runs."""
        rels = [m.get("path") or "" for m in batch.image_meta] + [
            f.get("path") or "" for f in batch.files]
        if not any(rels):
            return
        from api.media.uploads import _push_upload_to_active_remote_sessions
        agent_dir = config.get_agent_dir(agent)
        for rel in filter(None, rels):
            try:
                await _push_upload_to_active_remote_sessions(agent, rel, agent_dir / rel)
            except Exception:
                logger.warning(f"queued turn: attachment {rel} not pushed again", exc_info=True)

    async def _deliver_queued(self, chat_id: str, taken: list) -> tuple[str, str]:
        """Messages this person queued in ``chat_id``, out as the chat's
        next turn with no socket driving it (``core/events/input_queue.
        deliver``, the chat's claim held by the caller): this connection
        object stands for the person, live or closed, so the turn takes the
        sender's own gates. Its sign-in re-read from the cookie it came with
        (a password change, a sign out everywhere, an expiry or a held gate
        refuses), the chat's open and drive rules, the budget and the pool
        cap, then the turn headless (``_start_new_stream`` with the
        producer that drains the chat queue), on a session that serves this
        person (a Shared-only session warmed by someone else is closed and
        re-warmed as them). Never a live terminal. Returns
        ``(DELIVERY_SENT, "")``, ``(DELIVERY_GATED, why)`` (the items go back
        with the card) or ``(DELIVERY_DEFERRED, why)`` (they wait for the
        next pump end or touch), from ``input_queue``."""
        if self._closed_by_revalidation:
            return input_queue.DELIVERY_GATED, "the sender's session was closed"
        if not await asyncio.to_thread(self._revalidate_session):
            return input_queue.DELIVERY_GATED, "the sender's sign-in ended"
        if self._held_gate:
            return input_queue.DELIVERY_GATED, "the sender must change their password or enrol two-factor first"
        chat = await run_db(task_store.get_chat, chat_id)
        if not chat:
            return input_queue.DELIVERY_GATED, "the chat is gone"
        from api.agents.chats import can_access_chat
        if not await run_db(can_access_chat, self._viewer_context(), chat):
            return input_queue.DELIVERY_GATED, "the sender may no longer open the chat"
        refusal = await self._deny_task_continue(chat_id, chat, quiet=True)
        if refusal:
            return input_queue.DELIVERY_GATED, refusal
        refusal = await self._sender_refusal(chat_id, chat)
        if refusal:
            return input_queue.DELIVERY_GATED, refusal
        if interactive_session.find_live_for_chat(chat_id) is not None:
            return input_queue.DELIVERY_DEFERRED, "a live terminal drives the chat"
        layer = await self._resolve_layer_for_chat_async(chat_id)
        sid = chat.get("session_id") or ""
        if layer is None or not sid:
            return input_queue.DELIVERY_DEFERRED, "the chat has no session to resume"
        shared_only = _vis.is_shared_only(chat.get("agent") or "")
        waited = False
        if shared_only and not await layer.is_session_alive(sid):
            # The sender check reads the live session: a machine in its
            # reconnect grace is waited for here, once, ahead of it.
            await grace_layer(sid, layer).wait_session_reconnect(sid)
            waited = True
        if (shared_only and await layer.is_session_alive(sid)
                and not self._session_serves_sender(sid)):
            logger.info(f"queued turn: the session of chat={chat_id[:8]} serves another "
                        f"sender, closed so the turn re-warms as its author")
            await layer.close_session(sid)
        batch = TurnInput.combine([qi.item for qi in taken])
        await self._push_batch_attachments(chat.get("agent") or "", batch)
        prompt, consumed = await self._prepared_prompt(chat_id, batch.cli_text,
                                                       new_text=batch.text)
        rows_first = asyncio.get_running_loop().create_future()
        try:
            pump = await self._start_new_stream(
                prompt, target_session_id=sid, target_chat_id=chat_id, target_layer=layer,
                images=batch.images or None, headless=True, rows_first=rows_first,
                reconnect_waited=waited,
            )
        except TurnDeferred:
            self._restore_abort_flags(chat_id, consumed)
            for qi in taken:
                qi.waiting = input_queue.WAITING_RECONNECT
            return input_queue.DELIVERY_DEFERRED, "the machine is reconnecting"
        if pump is None:
            self._restore_abort_flags(chat_id, consumed)
            return input_queue.DELIVERY_DEFERRED, "the turn could not start"
        logger.info(f"WS dashboard: {len(taken)} queued message(s) out as the next turn, "
                    f"chat={chat_id[:8]}, headless as {(self.user_sub or '-')[:8]}")
        self._announce_sent(chat_id, taken, rows_first)
        return input_queue.DELIVERY_SENT, ""

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
                from api.agents.chats import can_access_chat
                allowed = await run_db(can_access_chat, self._viewer_context(), chat)
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

        # The chat's turn starters take turns: with a pump driving the chat,
        # or another starter bringing one up (a queued message's delivery, a
        # teammate's send), a person's message waits in the chat's queue and
        # this socket attaches to the turn that runs. A re-send of a message
        # a turn already took is answered once.
        q = None
        if not server_kick and not artifact_framed and self.chat_id:
            q = await input_queue.loaded(self.chat_id)
            queue_id = str(msg.get("queue_id") or "")
            if queue_id and queue_id in q.accepted:
                await self._send({"type": wire.DONE, "chat_id": self.chat_id})
                return
        claim = None
        if q is not None:
            claim = q.try_claim()
            if claim is None:
                item = await self._prepare_turn_input(text, images, files)
                if item is None:
                    await self._send({"type": wire.DONE, "chat_id": self.chat_id})
                    return
                holder = grace_layer(self.session_id or "", self.layer)
                reconnecting = bool(self.session_id and holder
                                    and holder.is_session_grace_held(self.session_id))
                await self._queue_for_next_turn(
                    self.chat_id, item, queue_id,
                    waiting=input_queue.WAITING_RECONNECT if reconnecting else "")
                if await self._enter_pump_loop() is None:
                    # No turn ran on this socket (the other starter deferred,
                    # or the chat is a call's): the message waits in the
                    # chat's queue under its chip and the composer is free.
                    await self._send({"type": wire.DONE, "chat_id": self.chat_id})
                return
            if queue_id:
                q.accepted.append(queue_id)
        try:
            await self._handle_claimed_chat(msg, q, claim)
        finally:
            if q is not None:
                q.release(claim)

    async def _handle_claimed_chat(self, msg: dict, q, claim: object | None) -> None:
        """The body of an idle send once the chat is this socket's to start
        (``q``: the chat's queue, its claim held; None for a server kick or
        an artifact-framed turn). The claim is released as soon as the pump
        is registered, before the socket streams the turn."""
        text = msg.get("text", "")
        images = msg.get("images", [])
        files = msg.get("files", [])
        server_kick = bool(msg.get("_server_kick"))
        artifact_framed = bool(msg.get("_artifact_framed"))

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

        heal = await self._heal_viewed_session(msg)
        if heal == HEAL_RECONNECTING:
            await self._queue_until_reconnect(msg, q)
            return
        if heal != HEAL_READY:
            return

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
        # The messages this person queued in the chat go first, as one turn
        # with this one (their rows land ahead of its row).
        taken = q.take(self.user_sub or "") if q is not None else []
        engine_input = TurnInput.combine([qi.item for qi in taken] + [turn_input]) \
            if taken else turn_input
        cli_text, attached_images = engine_input.cli_text, engine_input.images

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

        _taken_rows = [{"queue_id": qi.queue_id, "text": qi.item.text,
                        "event_data": json.dumps(qi.item.event_meta) if qi.item.event_meta else "",
                        "author_sub": qi.author_sub} for qi in taken]
        if taken:
            q.accepted.extend(qi.queue_id for qi in taken)

        def _send_job() -> dict:
            out = {"prev_author": "", "title_set": False, "rec": None, "cancelled": "",
                   "focus": "", "taken_ids": [], "consumed": None}
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
            if _taken_rows:
                out["taken_ids"] = task_store.accept_chat_inputs(_cid, _taken_rows)
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
                out["consumed"] = {"last_turn_aborted": True,
                                   "last_abort_graceful": graceful_abort}
                if not graceful_abort:
                    out["cancelled"] = self._build_cancelled_context(
                        _cid, new_rows=1 + len(_taken_rows or ()))
            return out

        sent = await chat_writer.submit(_cid, _send_job, label="chat_send")
        prev_author = sent["prev_author"]
        chat_rec = sent["rec"]
        if taken:
            queued_batch = TurnInput.combine([qi.item for qi in taken])
            input_queue.fan_out(_cid, {"type": wire.QUEUE_SENT,
                                       "queue_ids": [qi.queue_id for qi in taken],
                                       "message_ids": sent["taken_ids"],
                                       "text": queued_batch.text,
                                       **queued_batch.frame_fields()},
                                authors=(self.user_sub,))
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
        try:
            pump = await self._start_new_stream(
                cli_text,
                target_session_id=self.session_id,
                target_chat_id=self.chat_id,
                target_layer=self.layer,
                images=attached_images or None,
            )
        except TurnDeferred:
            # The machine dropped between the heal and the start: the row is
            # written, so the person is told to send it again.
            self._restore_abort_flags(_cid, sent["consumed"])
            if q is not None:
                q.release(claim)
            await self._send_reconnecting_line(self.chat_id or "")
            await self._send({"type": wire.DONE, "chat_id": self.chat_id or ""})
            return
        if q is not None:
            q.release(claim)
        if pump:
            await self._enter_pump_loop()
            # Ephemeral "turn done" is now fired from pump cleanup (covers WS death case).
            # What the chat's queue holds goes out from the loop's way out
            # (_drain_input_queue) or the pump's end (input_queue).
        else:
            # Pump creation failed (dead session, error) — reset frontend streaming state
            await self._send({"type": wire.DONE, "chat_id": self.chat_id or ""})

    def _maybe_stop_and_send(self, pump) -> None:
        """Typed message queued onto a busy turn → graceful stop-and-send.

        Fires only on pumps OWNED by ``_start_new_stream`` (``stop_and_send``
        flags present — its producer drains the chat's queue); foreign
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
        if ((not input_queue.peek(pump.chat_id) and not pump.message_queue)
                or _active_pumps.get(pump.chat_id) is not pump):
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
        from api.agents.chats import can_access_chat

        def _read_job() -> tuple[str, str] | None:
            # Access gate (the resume gate's rule) + the read mark in ONE
            # executor job.
            chat = task_store.get_chat(cid)
            if not chat or not can_access_chat(_viewer, chat):
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
