"""The client-message dispatcher: ONE table (``ws.wire_events.INBOUND``)
walked by both socket loops — the between-turns loop in ``ws/dashboard.py``
and the streaming loop in ``ws/dashboard_chat_stream.py``.

Every inbound message name maps to one ``_on_<name>`` method here, called
as ``handler(msg)`` between turns and ``handler(msg, stream=StreamCtx)``
while a turn streams; the table's ``mid_stream`` policy says which
messages the streaming loop hands to the handler (``SAME``), which make it
detach and hand the message back (``DETACH``), which end it (``END``) and
which it drops until the turn ends (``DROP``). A handler whose two halves
differ carries both under ``if stream is not None`` — the frames each half
emits, and their ``chat_id`` tags, are the golden masters'
(``tests/session/test_ws_dashboard_*.py``).

ClientMessageDispatcher is a mixin of ``DashboardConnection`` (ws/dashboard.py) — methods run
with the connection's full attribute state; nothing here is standalone.
"""

import asyncio
import contextlib
import base64
import functools
import json
import logging
from typing import NamedTuple

import config
from core import placement
from storage import database as task_store
from storage.pg import run_db
from storage.automation import notification_store
from services.notifications import notification_manager
from core.events import chat_writer
from core.session.session_state import (
    set_session_mode,
    resolve_permission,
    resolve_question,
    resolve_location,
    set_user_tz,
    set_session_user_tz,
    clear_session_liveness,
)
from core.session.session_manager import get_layer_capabilities, resolve_execution_path
from core.events.common_events import TurnInput
from core.events.stream_pump import _active_pumps, _pending_permissions
from core.session import session_kind, visibility as _vis, interactive_session
from auth import roles
from ws import wire_events as wire

logger = logging.getLogger("claude-proxy")


class StreamCtx(NamedTuple):
    """What the streaming loop hands a handler: the pump, this socket's
    subscription queue on it, and the viewed-chat tag every frame the loop
    emits carries (``self.chat_id or pump.chat_id``, computed once)."""

    pump: object
    queue: asyncio.Queue
    chat_id: str


def _question_answers_to_text(answers: dict) -> str:
    """Flatten a structured answers map ``{<qid>: {"answers": [...]}}`` to the
    text the FREE-TEXT question path sends (dashboard QuestionDialog's Claude
    branch: selected labels joined by ``", "``, one question per line). Only the
    fallback below uses it — a delivered answer goes to the held request as the
    verbatim map.
    """
    lines: list[str] = []
    for entry in (answers or {}).values():
        vals = entry.get("answers") if isinstance(entry, dict) else None
        if not isinstance(vals, list):
            continue
        joined = ", ".join(str(v).strip() for v in vals if str(v).strip())
        if joined:
            lines.append(joined)
    return "\n".join(lines)


class ClientMessageDispatcher:
    """The client-message handlers, one per inbound name, reached through
    ``wire.INBOUND`` by both socket loops."""

    async def _dispatch_client_message(self, msg: dict) -> str | None:
        """The between-turns walk: the table names the handler; an unknown
        name is refused with the same frame it always was."""
        msg_type = msg.get("type", "")
        spec = wire.INBOUND.get(msg_type)
        if spec is None:
            await self._send_error(f"Unknown message type: {msg_type}")
            return None
        return await getattr(self, spec.handler)(msg)

    # -- the session lifecycle ----------------------------------------------

    async def _on_pre_warmup(self, msg: dict, *, stream: StreamCtx | None = None):
        # Background so the dispatcher can keep processing the user's
        # next click — the FIRST chat-history click after switching to a
        # remote agent was waiting 5–10s for the eager pre_warmup's
        # satellite-side session start + MCP sync to finish in front of
        # it. Awaiting in-flight pre_warmup is handled in _handle_warmup
        # (so the first send_message still reuses the pre-warmed session).
        if self._pre_warmup_task and not self._pre_warmup_task.done():
            self._pre_warmup_task.cancel()
        self._pre_warmup_task = asyncio.create_task(self._handle_pre_warmup(msg))
        return None

    async def _on_warmup(self, msg: dict, *, stream: StreamCtx | None = None):
        prev_sid = self.session_id
        await self._handle_warmup(msg)
        self._view_only = bool(await self._deny_task_continue(self.chat_id, quiet=True))
        # Same-chat warm: a fresh sid supersedes the previous one.
        self._register_notify_queue(replaces=prev_sid)
        count = await asyncio.to_thread(notification_store.get_unread_count, self.user_sub)
        await self._send({"type": wire.NOTIFICATION_COUNT, "count": count})
        return None

    async def _on_resume_chat(self, msg: dict, *, stream: StreamCtx | None = None):
        await self._handle_resume_chat(msg)
        self._register_notify_queue()
        # If the resumed chat has an active pump, attach to it
        await self._enter_pump_loop()
        # Reconnected mid-background-run (turn already ended, bg subagents
        # still finishing) → relaunch the monitor so the review nudge still
        # fires. Idempotent: a no-op if one is already running or none pending.
        self._ensure_bg_monitor()
        return None

    async def _on_implement_plan(self, msg: dict, *, stream: StreamCtx | None = None):
        await self._handle_implement_plan(msg)
        self._view_only = bool(await self._deny_task_continue(self.chat_id, quiet=True))
        self._register_notify_queue()
        return None

    async def _on_close(self, msg: dict, *, stream: StreamCtx | None = None):
        logger.info(f"WS dashboard close: session={self.session_id}, chat={self.chat_id}")
        return wire.HANDLED_CLOSE

    async def _on_ping(self, msg: dict, *, stream: StreamCtx | None = None):
        await self._pong()
        return None

    # -- the turn: send, queue, steer, abort ---------------------------------

    async def _on_chat(self, msg: dict, *, stream: StreamCtx | None = None):
        if stream is not None:
            pump = stream.pump
            text = msg.get("text", "")
            images = msg.get("images") or []
            files = msg.get("files") or []
            # A message for another chat never steers or queues into this
            # stream (a socket a reconnect left on another live chat).
            msg_cid = msg.get("chat_id") or ""
            if (text or images or files) and msg_cid and msg_cid not in (stream.chat_id, pump.chat_id):
                await self._send_error("This message is for a chat this connection "
                                       "is not on. Reopen the chat and send it again.")
                await self._send({"type": wire.DONE, "chat_id": msg_cid})
                return None
            # Same task continue-gate the between-turns/warmup/
            # permission paths enforce — WITHOUT it a viewer of a
            # shared-only agent's task chat could steer/queue into a
            # running task run they're not allowed to continue
            # (Codex steer injects into the live turn). Applies only
            # to task-{run} chats; a no-op for regular chats.
            if ((text or images or files)
                    and await self._deny_task_continue(pump.chat_id)):
                text, images, files = "", [], []
            if text or images or files:
                # The busy send carries exactly what an idle send does:
                # the photos are saved and the files validated NOW, the
                # engine text gets their paths, the row gets the meta
                # (the pump's session: an attach viewer may hold a
                # different session id than the streaming turn).
                item = await self._prepare_turn_input(
                    text, images, files, session_id=pump.session_id,
                )
                if item is None:
                    return None
                # Steer-first: engines that support it (Codex
                # turn/steer) take the message INTO the running
                # turn — delivered exactly-once on accept, so it
                # must never also enter the queue. The user row
                # persists immediately (it is part of this turn's
                # context; the pump's turn blocks save after it,
                # matching the interactive tailers' mid-turn user
                # rows). Plan-implement enqueues use the
                # plan_review handler and never steer.
                steered = False
                if (self.session_id and self.layer
                        and getattr(pump, "source_type", session_kind.DASHBOARD.source_type)
                        in session_kind.STEERABLE_SOURCE_TYPES):
                    steered = bool(await self.layer.steer(self.session_id, item.cli_text))
                # Queued behind the turn, the message cuts it: a check
                # round that started before it is cancelled and the
                # turn is not continued (CHECKS.md). A steered message
                # is part of the running turn. The pump's session: an
                # attach viewer may hold a different one.
                if not steered and pump.session_id:
                    from core.session import session_events
                    session_events.note_user_message(pump.session_id)
                # Either way the message re-targets the end-of-turn
                # alert to this device.
                notification_manager.set_chat_turn_origin(
                    self.user_sub, pump.chat_id, self.notify_connection_id,
                )
                if steered:
                    _meta = item.event_meta
                    await chat_writer.submit(
                        pump.chat_id,
                        functools.partial(task_store.add_chat_message,
                                          pump.chat_id, "user", item.text,
                                          event_data=json.dumps(_meta) if _meta else "",
                                          author_sub=self.user_sub),
                        label="steered_row",
                    )
                    await self._send({"type": wire.STEERED, "text": item.text,
                                      "chat_id": stream.chat_id, **item.frame_fields()})
                else:
                    idx = pump.queue_message(item)
                    if idx < 0:
                        await self._send_error(
                            "Too many queued messages — wait for the "
                            "current turn to finish.")
                    else:
                        await self._send({"type": wire.QUEUED, "index": idx, "text": item.text,
                                          "chat_id": stream.chat_id, **item.frame_fields()})
                        # Stop-and-send (Claude CLI headless): fire a
                        # graceful-only interrupt so the producer's
                        # queue drain delivers this message as the
                        # next turn in seconds, not at turn end.
                        self._maybe_stop_and_send(pump)
            return None
        if self.streaming:
            # Queue the message (capped: a client can't grow this list
            # without bound by spamming `chat` while a turn streams).
            text = msg.get("text", "")
            images = msg.get("images") or []
            files = msg.get("files") or []
            if text or images or files:
                if len(self.message_queue) >= 64:
                    await self._send_error(
                        "Too many queued messages — wait for the current "
                        "turn to finish.")
                else:
                    item = await self._prepare_turn_input(text, images, files)
                    if item is None:
                        return None
                    self.message_queue.append(item)
                    await self._send({"type": wire.QUEUED, "index": len(self.message_queue) - 1,
                                      "text": item.text, **item.frame_fields()})
        else:
            await self._handle_chat(msg)
        return None

    async def _on_cancel_queued(self, msg: dict, *, stream: StreamCtx | None = None):
        # The removed item's text AND attachment meta go back: the
        # dashboard returns both to the composer.
        idx = msg.get("index", -1)
        if stream is not None:
            if await self._deny_task_continue(stream.pump.chat_id):
                return None
            item = stream.pump.cancel_queued(idx)
            if item is not None:
                await self._send({"type": wire.QUEUE_REMOVED, "index": idx, "text": item.text,
                                  "chat_id": stream.chat_id, **item.frame_fields()})
            return None
        if 0 <= idx < len(self.message_queue):
            item = self.message_queue.pop(idx)
            await self._send({"type": wire.QUEUE_REMOVED, "index": idx, "text": item.text,
                              **item.frame_fields()})
        return None

    async def _on_cancel_all_queued(self, msg: dict, *, stream: StreamCtx | None = None):
        if stream is not None:
            if await self._deny_task_continue(stream.pump.chat_id):
                return None
            combined = stream.pump.cancel_all_queued()
            await self._send({"type": wire.QUEUE_CLEARED, "text": combined, "chat_id": stream.chat_id})
            return None
        combined = "\n\n".join(q.text for q in self.message_queue) if self.message_queue else ""
        self.message_queue.clear()
        await self._send({"type": wire.QUEUE_CLEARED, "text": combined})
        return None

    async def _on_abort(self, msg: dict, *, stream: StreamCtx | None = None):
        # Stopping a task run's turn advances the run: the continue-gate
        # (the REST cancel route asks the same of its caller).
        if await self._deny_task_continue(stream.pump.chat_id if stream is not None else self.chat_id):
            return None
        if stream is not None:
            pump = stream.pump
            logger.info(f"WS dashboard: abort via pump, session={self.session_id}")
            # Layer abort FIRST: on the graceful path (Claude
            # control_request interrupt / Codex turn/interrupt) the
            # producer is the sole consumer of the closing turn's
            # tail events and must stay alive — the pump runs to
            # PRODUCER_DONE and persists the partial turn; the CLI
            # layer's watchdog falls back to killpg if the turn
            # doesn't close. Hard path keeps today's cancel order.
            # Stop means stop: a graceful abort still ends the turn with
            # its DONE, and no check may judge it and start a fix round
            # (noted first: the turn can close while the abort awaits).
            if pump.session_id:
                from core.session import session_events
                session_events.note_user_message(pump.session_id)
            graceful = False
            if self.session_id and self.layer:
                graceful = bool(await self.layer.abort(self.session_id))
            # Queued messages never survive an abort (the user asked
            # everything to stop): without the clear, the graceful
            # producer's post-turn drain would run them as new turns
            # and the hard path silently dropped them (pre-existing).
            _dropped_q = pump.cancel_all_queued()
            if _dropped_q:
                await self._send({"type": wire.QUEUE_CLEARED,
                                  "text": _dropped_q,
                                  "chat_id": stream.chat_id})
            if not graceful:
                pump.abort()
            pump.detach(stream.queue)
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
                    _abort_caps = get_layer_capabilities(_abort_path)
                    if _abort_caps and _abort_caps.runtime.hard_abort_kills_process:
                        clear_session_liveness(self.session_id, reason="abort")
            await self._send({"type": wire.ABORTED, "chat_id": stream.chat_id})
            self.pending_control_requests.clear()
            return wire.HANDLED_BREAK
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
            await self._send({"type": wire.ABORTED, "chat_id": self.chat_id or ""})
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
            # Aborting someone else's turn is driving their session.
            if not await self._may_drive_terminal(isess):
                return None
            isess.interrupt_turn()
            await self._send({"type": wire.ABORTED, "chat_id": self.chat_id or ""})
            return None
        # Non-streaming abort: no attached pump, but a detached pump may be
        # mid-turn and the process may be running. Layer abort FIRST — the
        # graceful path (Claude interrupt / Codex turn/interrupt) keeps the
        # producer alive so the detached pump persists the partial turn;
        # the hard path cancels it as before (the streaming half above).
        self.implementing_plan = ""
        graceful = False
        if self.session_id and self.layer:
            from core.session import session_events
            session_events.note_user_message(self.session_id)
            graceful = bool(await self.layer.abort(self.session_id))
        pump = _active_pumps.get(self.chat_id)
        if pump and not pump.is_done:
            _dropped_q = pump.cancel_all_queued()
            if _dropped_q:
                await self._send({"type": wire.QUEUE_CLEARED, "text": _dropped_q})
            if not graceful:
                pump.abort()
        # The connection's own between-turns queue dies with the abort too
        # (pending artifact interactions included — never delivered, never
        # persisted).
        self.artifact_queue.clear()
        if self.message_queue:
            _dropped_c = "\n\n".join(q.text for q in self.message_queue)
            self.message_queue.clear()
            await self._send({"type": wire.QUEUE_CLEARED, "text": _dropped_c})
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
                _abort_caps = get_layer_capabilities(_abort_path)
                if _abort_caps and _abort_caps.runtime.hard_abort_kills_process:
                    clear_session_liveness(self.session_id, reason="abort")
        await self._send({"type": wire.ABORTED, "chat_id": self.chat_id or ""})
        return None

    # -- the prompts: permission, question, location, plan review -----------

    async def _on_permission_response(self, msg: dict, *, stream: StreamCtx | None = None):
        # Resolve hook-based permission (unblocks the long-poll in hook endpoint).
        if stream is not None:
            if await self._may_resolve_permission(msg["request_id"]):
                resolve_permission(msg["request_id"], msg.get("approved", True))
                for sid, pd in list(_pending_permissions.items()):
                    if pd.get("request_id") == msg["request_id"]:
                        del _pending_permissions[sid]
                        break
                await stream.pump.resolve_active_permission()
            return None
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
        return None

    async def _on_question_response(self, msg: dict, *, stream: StreamCtx | None = None):
        if stream is not None:
            # Codex request_user_input answer — the held turn resumes.
            # Validate the answerer drives this session, resolve the
            # waiter with the answers map, then ADVANCE the pump's
            # permission slot (else a later prompt in the same held turn
            # buffers forever + a reconnect re-renders the answered card).
            if await self._may_resolve_permission(msg["request_id"]):
                resolve_question(msg["request_id"], msg.get("answers") or {})
                for sid, pd in list(_pending_permissions.items()):
                    if pd.get("request_id") == msg["request_id"]:
                        del _pending_permissions[sid]
                        break
                await stream.pump.resolve_active_permission()
            return None
        # Codex request_user_input answer arriving between turns (safety net;
        # a held question normally resolves mid-stream).
        if await self._may_resolve_permission(msg["request_id"]):
            delivered = resolve_question(msg["request_id"], msg.get("answers") or {})
            for sid, pd in list(_pending_permissions.items()):
                if pd.get("request_id") == msg["request_id"]:
                    del _pending_permissions[sid]
                    break
            if not delivered:
                await self._question_answer_fallback(msg)
        return None

    async def _on_location_response(self, msg: dict, *, stream: StreamCtx | None = None):
        # Only a connection that views a session and may drive its chat
        # answers for it (a read-only viewer of a shared chat never does).
        if not self.session_id or self._view_only or await self._deny_task_continue(
                self.chat_id, quiet=True):
            logger.info("WS dashboard: location answer dropped (not the driving connection)")
            return None
        if not resolve_location(msg["request_id"], {
            "lat": msg.get("lat"),
            "lng": msg.get("lng"),
            "accuracy": msg.get("accuracy"),
            "error": msg.get("error"),
        }, session_id=self.session_id):
            logger.info("WS dashboard: location answer dropped (not the asked session)")
        return None

    async def _on_plan_review_response(self, msg: dict, *, stream: StreamCtx | None = None):
        if stream is None:
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
        resolve_permission(msg["request_id"], approved)
        for sid, pd in list(_pending_permissions.items()):
            if pd.get("request_id") == msg["request_id"]:
                del _pending_permissions[sid]
                break
        if stream is not None:
            pump = stream.pump
            await pump.resolve_active_permission()
            # Save the user's action in the DB turn block
            req_id = msg.get("request_id", "")
            for tb in pump._turn_blocks:
                if tb.get("type") == wire.PLAN_REVIEW and tb.get("request_id") == req_id:
                    tb["action"] = action
                    break
            # Mode/plan-status writes ride the chat lane so they
            # stay ordered with the pump's own plan-mode write. The
            # mode_changed frames of this tail carry no chat tag.
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
                await self._send({"type": wire.MODE_CHANGED, "mode": restored_mode})
            if action == "implement_accept_edits":
                self.pending_control_requests.append(("set_permission_mode", {"mode": "acceptEdits"}))
                chat_writer.submit(
                    self.chat_id,
                    functools.partial(task_store.update_chat, self.chat_id,
                                      permission_mode="acceptEdits"),
                    label="plan_implement_mode",
                )
                await self._send({"type": wire.MODE_CHANGED, "mode": "acceptEdits"})
                pump.queue_message(TurnInput("Please implement the plan now."))
                pump.implementing_plan = plan_fn
            elif action == "implement_default":
                # Pinned asymmetry: no control request is queued here (the
                # between-turns half changes the mode through the layer).
                chat_writer.submit(
                    self.chat_id,
                    functools.partial(task_store.update_chat, self.chat_id,
                                      permission_mode="default"),
                    label="plan_implement_mode",
                )
                await self._send({"type": wire.MODE_CHANGED, "mode": "default"})
                pump.queue_message(TurnInput("Please implement the plan now."))
                pump.implementing_plan = plan_fn
            return None
        if self.chat_id and plan_fn and action == "reject":
            task_store.update_chat_plan_status(self.chat_id, plan_fn, "rejected")
            # Restore pre-plan mode + notify frontend
            restored_mode = self.pre_plan_mode_holder[0]
            task_store.update_chat(self.chat_id, permission_mode=restored_mode)
            await self._send({"type": wire.MODE_CHANGED, "mode": restored_mode})
        if action == "implement_accept_edits":
            await self._handle_mode_change({"mode": "acceptEdits", "chat_id": self.chat_id})
            self.implementing_plan = plan_fn
            # If session is dead (stale plan_review), queue implement for after warmup
            if not self.session_id:
                self.message_queue.append(TurnInput("Please implement the plan now."))
                logger.info(f"WS dashboard: queued implement message for dead session, plan={plan_fn}")
        elif action == "implement_default":
            await self._handle_mode_change({"mode": "default", "chat_id": self.chat_id})
            self.implementing_plan = plan_fn
            if not self.session_id:
                self.message_queue.append(TurnInput("Please implement the plan now."))
                logger.info(f"WS dashboard: queued implement message for dead session, plan={plan_fn}")
        return None

    # -- the backchannel: artifacts and app actions --------------------------

    async def _on_artifact_interaction(self, msg: dict, *, stream: StreamCtx | None = None):
        if stream is not None:
            # display_ui backchannel mid-turn: QUEUE ONLY — page
            # events never steer a running turn (lower authority
            # than the user typing). Validation mirrors the
            # between-turns handler.
            from ws import artifact_interactions as _ai
            pump = stream.pump
            a_token = str(msg.get("token") or "")
            a_chat = str(msg.get("chat_id") or "")
            a_frame: dict = {"type": wire.ARTIFACT_ACK, "token": a_token}
            if not a_chat or a_chat != pump.chat_id:
                a_frame.update(status="denied", reason="not the viewed chat")
            elif refusal := await self._deny_task_continue(a_chat):
                a_frame.update(status="denied", reason=refusal)
            elif pump._meeting_agent or await run_db(task_store.get_active_meeting_for_chat, a_chat):
                a_frame.update(status="unavailable", reason="meeting in progress")
            else:
                interaction, a_err = _ai.validate_interaction(
                    a_chat, a_token,
                    str(msg.get("title") or ""),
                    msg.get("payload"),
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
            return None
        # display_ui backchannel send (idle → framed turn; streaming →
        # queued to the boundary). Validation + acks live in the handler.
        await self._handle_artifact_interaction(msg)
        return None

    async def _on_app_action(self, msg: dict, *, stream: StreamCtx | None = None):
        if stream is not None:
            # App send_prompt mid-turn: QUEUE ONLY — same
            # never-steer rule as artifact interactions (the
            # approved template doesn't upgrade page events to
            # steering authority). Validation mirrors the
            # between-turns handler.
            from ws import artifact_interactions as _ai
            pump = stream.pump
            ap_id = str(msg.get("app_id") or "")
            ap_action = str(msg.get("action_id") or "")
            ap_chat = str(msg.get("chat_id") or "")
            ap_frame: dict = {"type": wire.APP_ACTION_ACK, "app_id": ap_id,
                              "action_id": ap_action}
            if not ap_chat or ap_chat != pump.chat_id:
                ap_frame.update(status="denied", reason="not the viewed chat")
            elif refusal := await self._deny_task_continue(ap_chat):
                ap_frame.update(status="denied", reason=refusal)
            elif pump._meeting_agent or await run_db(task_store.get_active_meeting_for_chat, ap_chat):
                ap_frame.update(status="unavailable", reason="meeting in progress")
            else:
                interaction, ap_err = _ai.validate_app_action(
                    ap_chat, self.agent_name or "", self.user_sub or "",
                    ap_id, ap_action, msg.get("args"),
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
            return None
        # Pinned app send_prompt action — same delivery rails,
        # gated on the user-approved manifest instead of a chat-bound
        # capability token.
        await self._handle_app_action(msg)
        return None

    # -- the interactive terminal --------------------------------------------

    async def _on_pty_attach(self, msg: dict, *, stream: StreamCtx | None = None):
        # The client's terminal has mounted + subscribed → attach the PTY
        # viewer NOW (the scrollback replay can't race the subscribe). Resolve
        # the connection's viewed interactive session; guard the chat matches
        # so a fast chat-switch can't attach the wrong terminal.
        isess = interactive_session.get(self.session_id) if self.session_id else None
        if isess is not None and isess.alive and isess.chat_id == msg.get("chat_id"):
            await self._attach_pty_viewer(isess)
        return None

    async def _on_pty_takeover(self, msg: dict, *, stream: StreamCtx | None = None):
        # Read-only viewer of another user's terminal asks for control.
        await self._handle_pty_takeover(msg)
        return None

    async def _on_pty_input(self, msg: dict, *, stream: StreamCtx | None = None):
        # Interactive (PTY) keystrokes → the connection's VIEWED session
        # (session_id, set on warmup_ready). Routing by session_id (not the
        # attach-set _pty_viewer_sid) means the cold-start first input still
        # lands before the pty_attach handshake. get() returns None for a
        # headless session → no-op. write_input resets the idle timer.
        isess = interactive_session.get(self.session_id) if self.session_id else None
        if isess is not None:
            if session_kind.is_task_chat_id(isess.chat_id) and not await self._may_drive_terminal(isess):
                return None
            try:
                if not isess.deliver_dashboard_input(
                    base64.b64decode(msg.get("data", "")),
                    composer=bool(msg.get("composer")),
                    sender_sub=self.user_sub,
                ):
                    await self._send_pty_read_only(isess)
            except Exception:
                logger.debug("pty_input decode/write failed", exc_info=True)
        return None

    async def _on_pty_resize(self, msg: dict, *, stream: StreamCtx | None = None):
        # Client terminal resize → SIGWINCH to the TUI (viewed session).
        # Gate on may_drive like pty_input/pty_attachments: a read-only
        # viewer resizing the controller's PTY would disrupt their TUI
        # rendering (SIGWINCH reflow).
        isess = interactive_session.get(self.session_id) if self.session_id else None
        if isess is not None and isess.may_drive(self.user_sub):
            if (session_kind.is_task_chat_id(isess.chat_id)
                    and await self._deny_task_continue(isess.chat_id)):
                return None
            try:
                isess.resize(int(msg.get("rows", 24)), int(msg.get("cols", 80)))
            except Exception:
                logger.debug("pty_resize failed", exc_info=True)
        return None

    async def _on_pty_attachments(self, msg: dict, *, stream: StreamCtx | None = None):
        # Interactive (PTY) photo/file attachments.
        # Reuse the normal-turn attachment pipeline: save base64 photos to the
        # agent's scope-correct workspace (+ push to any remote satellite) and
        # build the prompt with sandbox-virtual paths the TUI's Read tool can
        # open, then type it into the live PTY (bracketed paste so the
        # multi-line path block lands as one input) and submit with Enter.
        isess = interactive_session.get(self.session_id) if self.session_id else None
        if isess is not None and isess.alive:
            # Refuse BEFORE the upload work — a read-only viewer must not
            # even write attachment files into the agent's workspace.
            if not await self._may_drive_terminal(isess):
                return None
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
                isess.deliver_dashboard_input(payload.encode("utf-8"), composer=True,
                                              sender_sub=self.user_sub)
            except Exception:
                logger.exception("pty_attachments failed")
        return None

    # -- the session's settings ----------------------------------------------

    async def _on_mode_change(self, msg: dict, *, stream: StreamCtx | None = None):
        await self._handle_mode_change(msg)
        return None

    async def _on_model_change(self, msg: dict, *, stream: StreamCtx | None = None):
        await self._handle_model_change(msg)
        return None

    async def _on_execution_mode_change(self, msg: dict, *, stream: StreamCtx | None = None):
        await self._handle_execution_mode_change(msg)
        return None

    async def _on_execution_mode_switch(self, msg: dict, *, stream: StreamCtx | None = None):
        await self._handle_switch_execution_mode(msg)
        return None

    async def _on_compact_context(self, msg: dict, *, stream: StreamCtx | None = None):
        # Compaction rewrites the run's context: the continue-gate.
        if await self._deny_task_continue(self.chat_id):
            return None
        await self._handle_compact_context()
        return None

    async def _on_move_chat(self, msg: dict, *, stream: StreamCtx | None = None):
        await self._handle_move_chat()
        return None

    async def _on_switch_engine(self, msg: dict, *, stream: StreamCtx | None = None):
        await self._handle_switch_engine(msg)
        return None

    async def _on_probe_liveness(self, msg: dict, *, stream: StreamCtx | None = None):
        # Lazy client-side liveness refresh (fired when the model dropdown
        # opens): headless idle-reap emits no frame, so without this the
        # cross-engine options never appear on a chat that died while
        # being viewed. Bound chat only — arbitrary ids aren't probeable.
        from ws.dashboard import chat_process_alive, task_run_active_async
        _cid = self.chat_id or ""
        _chat = await run_db(task_store.get_chat, _cid) if _cid else None
        _alive = False
        if _chat:
            _pump = _active_pumps.get(_cid)
            # task_run_active: a parked/spawning/recovering task run has
            # no live session yet — the picker must still stay locked.
            _alive = bool(_pump and not _pump.is_done) or \
                await task_run_active_async(_cid) or \
                await chat_process_alive(_chat)
        await self._send({"type": wire.LIVENESS, "chat_id": _cid,
                          "process_alive": _alive})
        return None

    # -- the connection's presence and its subscriptions ---------------------

    async def _on_chat_read(self, msg: dict, *, stream: StreamCtx | None = None):
        # Read receipts arrive mid-turn too (the client marks the chat read
        # on every history load) — DB + fan-out only, stream-safe.
        await self._handle_chat_read(msg)
        return None

    async def _on_client_info(self, msg: dict, *, stream: StreamCtx | None = None):
        platform = msg.get("platform", "web")
        notification_manager.set_connection_platform(self.user_sub, self.notify_connection_id, platform)
        time_zone = msg.get("time_zone")
        if time_zone:
            set_user_tz(self.user_sub, time_zone)
            # The session's zone is the driver's: a read-only viewer's
            # never replaces it.
            if self.session_id and not self._view_only:
                set_session_user_tz(self.session_id, time_zone)
        logger.info(
            f"WS dashboard client_info: user={self.user_sub}, platform={platform}, tz={time_zone or '-'}"
        )
        return None

    async def _on_user_active(self, msg: dict, *, stream: StreamCtx | None = None):
        notification_manager.set_connection_active(self.user_sub, self.notify_connection_id, True)
        return None

    async def _on_user_idle(self, msg: dict, *, stream: StreamCtx | None = None):
        notification_manager.set_connection_active(
            self.user_sub, self.notify_connection_id, False,
            away=bool(msg.get("away")),
        )
        return None

    async def _on_focus(self, msg: dict, *, stream: StreamCtx | None = None):
        # What this tab shows (chat, home, or which app) — read by the
        # next turn's context line (APPS.md "Live apps"). Mid-turn too: the
        # user opens an app while the agent works.
        notification_manager.set_connection_focus(
            self.user_sub, self.notify_connection_id, msg,
        )
        return None

    async def _on_catalog_subscription(self, msg: dict, *, stream: StreamCtx | None = None):
        # An app on this tab asked for (or dropped) a platform feed:
        # deltas reach only subscribed connections (APPS.md
        # "Platform catalog"). Mid-turn too: an app opened mid-turn
        # subscribes now, not after the turn.
        from api.apps import catalog
        catalog.handle_subscription(self.user_sub, self.notify_connection_id, msg)
        return None

    async def _question_answer_fallback(self, msg: dict) -> None:
        """Deliver a structured question answer that reached NO live waiter.

        ``resolve_question`` returning False means the held
        ``item/tool/requestUserInput`` server-request is gone — its session was
        reaped (or released) while the human was deciding — so the parked turn
        can never resume and the answer would be silently lost (the app-server
        write would hit a closed transport; live on T1, 2026-08-06). The answer
        is a HUMAN's input, so re-send it as an ordinary composer message on the
        chat: that takes ``_handle_chat``'s dead-session branch, which does the
        registry-aware single-flight revival, persists the text as a user
        message and runs the turn.
        """
        text = _question_answers_to_text(msg.get("answers") or {})
        chat_id = msg.get("chat_id") or self.chat_id or ""
        if not text or not chat_id:
            logger.warning(
                "WS dashboard question_response: no live waiter for request %s "
                "and nothing to replay (chat=%s)", str(msg.get("request_id"))[:8], chat_id,
            )
            return
        logger.warning(
            "WS dashboard question_response: no live waiter for request %s — "
            "replaying the answer as a chat message on chat=%s (session reaped "
            "while the question was parked)",
            str(msg.get("request_id"))[:8], chat_id,
        )
        if self.streaming:
            self.message_queue.append(TurnInput(text))
            await self._send({
                "type": wire.QUEUED, "index": len(self.message_queue) - 1, "text": text,
            })
            return
        await self._handle_chat({"text": text, "chat_id": chat_id})

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
        from auth.providers import acting_role_of

        logger.info(
            "WS dashboard: move_chat requested chat=%s session=%s streaming=%s",
            self.chat_id or "?", (self.session_id or "?")[:8], self.streaming,
        )
        from storage.pg import run_db
        chat = await run_db(task_store.get_chat, self.chat_id) if self.chat_id else None
        if not chat:
            await self._send({"type": wire.ERROR, "message": "Chat not found."})
            return
        agent = chat.get("agent") or ""
        role = await run_db(acting_role_of, self.user_sub, agent)
        if chat.get("user_sub") != self.user_sub and not roles.is_admin(role):
            await self._send({"type": wire.ERROR,
                              "message": "Only the chat owner can move it."})
            return
        if self._warmup_task and not self._warmup_task.done():
            await self._send({"type": wire.ERROR,
                              "message": "The chat is still getting ready — "
                                         "try again in a moment."})
            return
        # A warmup from ANOTHER connection (second tab) registers globally but
        # is invisible to the connection-scoped check above — its tail would
        # stamp a session spawned for the OLD pin onto the moved row.
        from core.session import warmup_registry as _wreg
        if _wreg.get(self.chat_id) is not None:
            await self._send({"type": wire.ERROR,
                              "message": "The chat is still getting ready — "
                                         "try again in a moment."})
            return
        pump = _active_pumps.get(self.chat_id) if self.chat_id else None
        if self.streaming or (pump and not pump.is_done):
            await self._send({"type": wire.ERROR,
                              "message": "Finish or stop the current turn "
                                         "before moving the chat."})
            return
        isess = (interactive_session.get(self.session_id)
                 if self.session_id else None)
        if isess is not None and isess.alive and isess.turn_open:
            await self._send({"type": wire.ERROR,
                              "message": "Finish or stop the current turn "
                                         "before moving the chat."})
            return

        pin = chat.get("execution_target") or ""
        resolved, _ = remote_store.resolve_execution_target(
            agent, self.user_sub, role,
        )
        if not resolved or placement.is_offline_sentinel(resolved):
            await self._send({"type": wire.ERROR,
                              "message": "The agent's current machine is "
                                         "offline — nowhere to move to."})
            return
        if not pin or pin == resolved:
            await self._send({"type": wire.ERROR,
                              "message": "This chat already runs on the "
                                         "agent's current target."})
            return

        def _label(target: str) -> str:
            if placement.is_local(target):
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
            await self._send({"type": wire.ERROR, "message": "Move failed."})
            return
        self.session_id = None
        logger.info(
            "WS dashboard: chat %s moved (%s -> %s) by %s",
            self.chat_id, pin, resolved, self.user_sub,
        )
        await self._send({
            "type": wire.CHAT_MOVED,
            "chat_id": self.chat_id,
            "new_target": resolved,
            "resolved_label": _label(resolved).removeprefix("the "),
        })

    async def _handle_switch_engine(self, msg: dict):
        """Cross-engine resume: flip the OPEN chat's execution engine + model
        so the next warmup fresh-spawns on the new engine with the DB-history
        digest (``pending_history_seed='engine_switch:<old>'``).

        Only for a DEAD session — while the process is alive the chat stays
        locked to its engine (the dropdown never offers foreign models). The
        chat row is authoritative for every resume read-site, so the rebind
        alone re-routes all subsequent spawns; the rebind is a CAS so a
        concurrent warmup/adopt that re-bound the row makes this a clean
        no-op. Normal chats: owner/admin only (move_chat parity). TASK-run
        chats (1.5): allowed once the RUN is over — follow-up turns resolve
        engine/model/seed from the chat row, so the switch governs exactly
        those; gates = run not pending/running (``task_run_active`` — the
        run lifecycle is invisible to session probes), the continue tier
        (``_task_continue_allowed``: agent-scope → editor+, user-scope →
        creator/admin), and TASK-identity credentials (agent scope runs on
        the platform pool, not the viewer's subs). Phone chats stay denied
        (their pipeline resolves the engine from agent config every call).
        Chats targeted by an ACTIVE continuation task are denied — a rebind
        NULLs session_id and the continuation's oneshot delivery would
        starve (1.5 self-wake design round, W2-F5)."""
        import json as _json
        from api.agents._common import _get_execution_paths
        from services.engines import subscription_pool
        from services.notifications import notification_manager as _nm
        from storage.agents import agent_store
        from storage.billing import subscription_store
        from core.session import history_seed, session_delivery, warmup_registry
        from core.session.session_manager import get_execution_layer
        from auth.providers import acting_role_of
        from ws.dashboard import chat_process_alive

        new_path = (msg.get("execution_path") or "").strip()
        new_model = (msg.get("model") or "").strip()

        async def _deny(reason: str, *, alive: bool | None = None):
            payload = {"type": wire.SWITCH_ENGINE_DENIED,
                       "chat_id": self.chat_id or "", "message": reason}
            if alive is not None:
                payload["process_alive"] = alive
            await self._send(payload)

        logger.info(
            "WS dashboard: switch_engine requested chat=%s -> %s/%s",
            self.chat_id or "?", new_path or "?", new_model or "?",
        )
        from storage.pg import run_db
        chat = await run_db(task_store.get_chat, self.chat_id) if self.chat_id else None
        if not chat:
            await _deny("Chat not found.")
            return
        cid = chat["id"]
        agent = chat.get("agent") or ""
        if session_kind.of_chat(chat) is session_kind.PHONE:
            await _deny("Phone chats can't switch engines — their calls "
                        "resolve the engine from the agent configuration.")
            return
        role = await run_db(acting_role_of, self.user_sub, agent)
        is_task_chat = session_kind.is_task_chat_id(cid)
        task_run = None
        if is_task_chat:
            from ws.dashboard import _task_continue_allowed, task_run_active_async
            if await task_run_active_async(cid):
                await _deny("The task run is still active — switching "
                            "engines is possible once it has finished.",
                            alive=True)
                return
            task_run = await run_db(task_store.get_run, session_kind.run_id_of_chat(cid))
            if task_run is not None:
                if not _task_continue_allowed(
                        task_run, effective_role=role, user_sub=self.user_sub):
                    await _deny("Switching this task chat's engine needs the "
                                "same access as continuing it (agent-scope: "
                                "editor or above; user-scope: the creator).")
                    return
            elif _vis.is_task_chat_owner(chat.get("user_sub")):
                # Run row aged out; the synthetic owner can't match any
                # viewer — apply the agent-scope continue tier.
                if not roles.can_edit(role):
                    await _deny("Switching this task chat's engine needs "
                                "editor access on the agent.")
                    return
            elif (chat.get("user_sub") != self.user_sub and not roles.is_admin(role)):
                await _deny("Only the chat owner can switch its engine.")
                return
        elif chat.get("user_sub") != self.user_sub and not roles.is_admin(role):
            await _deny("Only the chat owner can switch its engine.")
            return
        # A chat wired as an active continuation target must keep its row
        # coherent with the continuation's delivery ladder — deny for BOTH
        # task-run chats and delegate worker chats (the latter pass the old
        # prefix deny already today).
        if await run_db(task_store.list_continuations_for_chat, cid):
            await _deny("This chat is the target of a scheduled follow-up — "
                        "its engine is managed by that task until the "
                        "follow-up completes.")
            return
        if ((self._warmup_task and not self._warmup_task.done())
                or warmup_registry.get(cid) is not None
                or session_delivery.oneshot_inflight(cid) is not None):
            await _deny("The chat is still getting ready — try again in a "
                        "moment.")
            return
        pump = _active_pumps.get(cid)
        if self.streaming or (pump and not pump.is_done):
            await _deny("Finish or stop the current turn before switching "
                        "engines.", alive=True)
            return
        if await chat_process_alive(chat):
            await _deny("The session is still active — switching engines is "
                        "possible once it has ended.", alive=True)
            return

        old_path_raw = chat.get("execution_path") or ""
        old_path = resolve_execution_path(agent, old_path_raw)
        if not new_path or new_path == old_path:
            await _deny("Pick a different AI engine to switch to.")
            return
        agent_rec = agent_store.get_agent(agent) or {}
        if new_path not in _get_execution_paths(agent_rec):
            await _deny("That AI engine isn't enabled for this agent.")
            return
        if is_task_chat:
            # The follow-up turn rebuilds under the TASK identity, not the
            # viewer: agent scope → platform pool only; user scope → the
            # CREATOR's credentials. Checking the viewer here would both
            # deny legitimate switches and green-light unusable ones.
            _scope = ((task_run or {}).get("scope")
                      or ("agent" if _vis.is_task_chat_owner(chat.get("user_sub"))
                          else "user"))
            if _scope == "agent":
                if not await asyncio.to_thread(
                        subscription_pool.layer_platform_configured, new_path):
                    await _deny("The platform pool has no credentials for "
                                "that AI engine — an admin must connect one "
                                "before agent-scope task chats can use it.")
                    return
            else:
                _creator = ((task_run or {}).get("created_by")
                            or chat.get("user_sub") or "")
                if not await asyncio.to_thread(
                        subscription_pool.user_can_run, new_path, _creator):
                    await _deny("The task's creator has no access to that "
                                "AI engine — its follow-up turns run on "
                                "their credentials.")
                    return
        elif not await asyncio.to_thread(
                subscription_pool.user_can_run, new_path, self.user_sub):
            await _deny("You don't have access to that AI engine — connect "
                        "it in your AI-engine settings first.")
            return
        try:
            _models = await asyncio.to_thread(
                subscription_store.list_models, new_path)
        except Exception:
            _models = []
        if not new_model or not any(
            m.get("enabled") and (m.get("model_id") or "") == new_model
            for m in _models
        ):
            await _deny("That model isn't available on the selected engine.")
            return

        # Teardown of any still-REGISTERED dead session: registry-membership
        # close (the stored path may be stale relative to where the session
        # actually lives), liveness badges, stale permission prompts, slot.
        old_sid = chat.get("session_id") or ""
        with contextlib.suppress(Exception):
            await interactive_session.close_for_chat(
                cid, reason="engine_switch")
        if old_sid:
            with contextlib.suppress(Exception):
                from api.agents.chats import _close_chat_session
                await _close_chat_session(chat)
            clear_session_liveness(old_sid, reason="engine_switch")
            _pending_permissions.pop(old_sid, None)
            from core.concurrency import release_chat_slot
            with contextlib.suppress(Exception):
                release_chat_slot(old_sid)

        # FINAL re-validation + CAS rebind. Everything from here to the
        # UPDATE is synchronous (no await), so no coroutine can interleave a
        # warmup between the re-check and the commit; a warmup that raced the
        # teardown above re-bound the row and fails the CAS instead.
        if (warmup_registry.get(cid) is not None
                or session_delivery.oneshot_inflight(cid) is not None):
            await _deny("The chat re-warmed while switching — try again.")
            return
        if is_task_chat:
            # Re-assert inside the no-await window: a scheduler fire that
            # admitted a new run for this chat between the gate above and
            # here must win (task_run_active is a synchronous DB read — no
            # coroutine interleaves before the CAS below).
            from ws.dashboard import task_run_active as _tra
            if _tra(cid):
                await _deny("A task run started while switching — try again "
                            "once it finishes.", alive=True)
                return
        _new_caps = get_layer_capabilities(new_path)
        _rebuilds = bool(_new_caps and _new_caps.behaviour.rebuilds_history_from_db)
        if _rebuilds:
            # An engine that rebuilds full DB history every turn never
            # consumes seeds: don't leave a flag to fire a stale digest on a
            # later hop back — and claim/persist any PRIOR pending notice
            # (moved:/machine_removed:) so its card isn't silently swallowed.
            if chat.get("pending_history_seed"):
                history_seed.consume_pending_seed_digest(cid, max_chars=0)
            pending_seed = ""
        else:
            pending_seed = f"engine_switch:{old_path}"
        if not task_store.rebind_chat_for_engine_switch(
            cid, new_path, new_model,
            expected_old_path=old_path_raw,
            expected_session_id=chat.get("session_id"),
            pending_seed=pending_seed,
        ):
            await _deny("The chat changed underneath — try again.")
            return

        if _rebuilds:
            # No seed will ever be consumed, so persist the switch card now.
            with contextlib.suppress(Exception):
                task_store.add_chat_message(
                    cid, "event", "",
                    event_type=wire.SYSTEM,
                    event_data=_json.dumps({
                        "type": wire.SYSTEM, "subtype": wire.SUBTYPE_SESSION_RESEEDED,
                        "message": history_seed.engine_switch_notice(old_path),
                        "machine_name": "", "reason": "engine_switch",
                    }),
                )
        self.session_id = None
        # Re-home the connection's layer so the next send resolves the NEW
        # engine (move_chat leaves this to the next resume; here the user
        # stays on the open chat and prompts immediately).
        with contextlib.suppress(Exception):
            self.layer = get_execution_layer(
                agent, execution_path=new_path, user_sub=self.user_sub,
                role=role, execution_target=chat.get("execution_target") or "",
            )
        logger.info(
            "WS dashboard: chat %s switched engine (%s -> %s, model=%s) by %s",
            cid, old_path, new_path, new_model, self.user_sub,
        )
        ack = {"type": wire.ENGINE_SWITCHED, "chat_id": cid,
               "execution_path": new_path, "model": new_model}
        await self._send(ack)
        # Sibling tabs (same owner) re-home their locked dropdowns; the
        # acting socket receives the frame twice — receivers are idempotent.
        _nm.broadcast_engine_switched(
            chat.get("user_sub") or "", cid, new_path, new_model, agent=agent,
        )

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
            await self._send({"type": wire.ERROR,
                              "message": "No active session — send a message "
                                         "first, then compact."})
            return
        pump = _active_pumps.get(self.chat_id) if self.chat_id else None
        if self.streaming or (pump and not pump.is_done):
            await self._send({"type": wire.ERROR,
                              "message": "Cannot compact while a turn is running."})
            return
        await self._send({"type": wire.CONTEXT_COMPACT, "phase": "started",
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
            await self._send({"type": wire.CONTEXT_COMPACT, "phase": "failed",
                              "chat_id": self.chat_id or ""})
            await self._send({"type": wire.ERROR,
                              "message": "Context compaction failed or is not "
                                         "supported by this engine."})
            return
        post_tokens = result.get("post_tokens")
        evt = {"type": wire.CONTEXT_COMPACT, "phase": "completed",
               "trigger": "manual", "post_tokens": post_tokens}
        if self.chat_id:
            # Persist the separator block (pump-shaped event row) and pin the
            # gauge so a reload shows the compacted size.
            task_store.add_chat_message(self.chat_id, "event", "",
                                        event_type=wire.CONTEXT_COMPACT,
                                        event_data=_json.dumps(evt))
            if post_tokens is not None:
                task_store.update_chat(self.chat_id, context_used=post_tokens)
        await self._send({**evt, "chat_id": self.chat_id or ""})
