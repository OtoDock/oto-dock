"""resume_chat: history replay, the visit path, restore and the lazy warmup
decision when the session is dead or missing.

``ChatResumeMixin`` is one of the mixins ``ws/dashboard_chat.py`` assembles into
``ChatController``; methods run with the connection's full attribute state
and reach the other mixins through ``self``. Nothing here is standalone.
"""

import asyncio
import functools
import json
import logging
from datetime import datetime, timezone
import config
from storage import database as task_store
from storage.pg import run_db
from core.events import chat_writer
from core.session.session_state import (
    get_session_mode,
    get_user_tz,
    set_session_user_tz,
)
from core.session.session_manager import resolve_execution_path
from core.events.stream_pump import (
    _active_pumps,
    _pending_permissions,
)
from core.session import warmup_registry, visibility as _vis, interactive_session
# Imported by ws/dashboard.py AFTER its helpers are defined —
# safe intra-unit circularity (see the class assembly there).
from ws.dashboard import (
    STALE_TURN_SECS,
    _CHAT_PAGE,
    _EXTERNAL_DRIVEN_SOURCES,
    _build_chat_restore,
    _role_and_layer,
    chat_process_alive,
    task_run_active,
)

logger = logging.getLogger("claude-proxy")


class ChatResumeMixin:
    """resume_chat: history replay, the visit path, restore and the lazy warmup"""

    async def _handle_resume_chat(self, msg: dict):
        self.promised_pump_chat = None  # every resume resets the previous promise
        self._detach_pty_viewer()  # leaving any interactive chat we were viewing

        cid = msg.get("chat_id", "")
        if not cid:
            await self._send_error("chat_id required")
            return

        chat = await run_db(task_store.get_chat, cid)
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
            def _inflight_job():
                return (*task_store.get_chat_messages_page(cid, _CHAT_PAGE),
                        _build_chat_restore(cid))

            # A lane job: the first prompt was queued on this chat's lane by
            # _persist_first_prompt — read it back behind that write.
            inflight_msgs, inflight_has_more, inflight_restore = await chat_writer.submit(
                cid, _inflight_job, label="inflight_history",
            )
            await self._send({
                "type": "chat_history",
                "chat_id": cid,
                "agent": chat["agent"],  # agent of record — URL normalization
                "messages": inflight_msgs,
                "has_more": inflight_has_more,
                "restore": inflight_restore,
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
                # A warmup is mid-spawn — the session is coming up; never
                # offer cross-engine options against it.
                "process_alive": True,
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
        _, self.layer = await run_db(
            _role_and_layer, self.user_sub, self.agent_name, chat, fallback_user=self.user,
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

        def _history_job():
            # A chat-lane job: the page read lands BEHIND the pump's queued
            # saves for this chat, so a resume right after a turn sees its
            # rows; the plans + panel-restore state ride the same job.
            if is_task_chat:
                msgs, more = task_store.get_chat_messages(cid), False
            else:
                msgs, more = task_store.get_chat_messages_page(cid, _CHAT_PAGE)
            return msgs, more, task_store.get_chat_plans(cid), _build_chat_restore(cid)

        messages, has_more, plans, restore = await chat_writer.submit(
            cid, _history_job, label="history_read",
        )

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
                                if rp._db_msg_cutoff_id is None  # start job pending: no row of that turn exists yet
                                or int(m["id"]) <= rp._db_msg_cutoff_id
                                or m.get("role") == "user"
                            ]
                        if extra_msgs:
                            messages.extend(extra_msgs)

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
                if pump._db_msg_cutoff_id is None  # start job pending: no row of this turn exists yet
                or int(m["id"]) <= pump._db_msg_cutoff_id
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
                     "restore": restore,
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
                     "mode": chat.get("permission_mode", "default"),
                     # Best-effort process liveness (in-memory only, no RPC) —
                     # gates the cross-engine model options client-side. An
                     # active pump also means alive (the attach below streams
                     # it); a pending/running TASK RUN counts as alive too
                     # (parked/spawning runs have no session yet but must
                     # keep the picker locked). The switch op re-checks
                     # authoritatively.
                     "process_alive": (
                         bool(pump and not pump.is_done)
                         or task_run_active(chat["id"])
                         or await chat_process_alive(chat)
                     )})

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
                **(await run_db(self._target_mismatch_fields, self.chat_id)),
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
                    **(await run_db(self._target_mismatch_fields, self.chat_id)),
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
                    **(await run_db(self._target_mismatch_fields, self.chat_id)),
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
                    await chat_writer.submit(
                        self.chat_id,
                        functools.partial(
                            task_store.add_chat_message, self.chat_id, "event", "",
                            event_type="permission_prompt",
                            event_data=json.dumps({
                                "type": "permission_prompt",
                                "request_id": stale_perm.get("request_id", ""),
                                "tool_name": stale_perm.get("tool_name", ""),
                                "tool_input": stale_perm.get("tool_input", {}),
                                "resolved": True, "approved": False,
                            }),
                        ),
                        label="stale_permission",
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
            **(await run_db(self._target_mismatch_fields, self.chat_id)),
        })
        # Lazy path — no pump, no session yet. Clear any persisted queue
        # on the client (rare but possible if user reloaded after a turn
        # finished but before sending again).
        await self._send({"type": "queue_snapshot", "chat_id": self.chat_id, "messages": []})
        logger.info(
            f"WS dashboard resume_chat (lazy): session dead/missing, "
            f"chat={self.chat_id}, agent={self.agent_name} — will warmup on first message"
        )
