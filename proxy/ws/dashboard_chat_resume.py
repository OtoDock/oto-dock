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
from core import placement
from storage import database as task_store
from storage.automation import run_status
from storage.pg import run_db
from core.events import chat_writer, input_queue, turn_ending
from core.events.common_events import CommonEvent, ERROR
from core.session.session_state import (
    _chat_streaming_state,
    get_session_mode,
    get_user_tz,
    set_session_user_tz,
)
from core.session.session_manager import resolve_execution_path
from core.events.stream_pump import (
    _active_pumps,
    _pending_permissions,
)
from core.session import session_kind, warmup_registry, interactive_session
from core.session import visibility as _vis
# Imported by ws/dashboard.py AFTER its helpers are defined —
# safe intra-unit circularity (see the class assembly there).
from ws.dashboard import (
    STALE_TURN_SECS,
    _CHAT_PAGE,
    _build_chat_restore,
    _role_and_layer,
    chat_process_alive,
    task_run_active_async,
)
from auth.providers import UserContext
from ws import wire_events as wire
from ws.dashboard_chat_text import grace_layer

logger = logging.getLogger("claude-proxy")


_NO_CUT = object()


def _cut_rows(rows: list[dict], cuts: dict[str, int | None], floors: dict[str, int],
              ) -> tuple[list[dict], dict[str, int]]:
    """The rows a history frame carries and the floors the next delta reads
    above. A chat in ``cuts`` has a live pump: its rows above the cutoff are
    withheld (user rows exempt) and never raise its floor, so they come
    with the next delta; a cutoff of None withholds nothing. A floor only
    rises: a chat with no row carried keeps the floor it had."""
    kept: list[dict] = []
    carried: dict[str, int] = {}
    for m in rows:
        chat = m.get("chat_id") or ""
        cut = cuts.get(chat, _NO_CUT)
        above = cut is not _NO_CUT and cut is not None and int(m["id"]) > cut
        if above and m.get("role") != "user":
            continue
        kept.append(m)
        if not above:
            carried[chat] = max(carried.get(chat, 0), int(m["id"]))
    return kept, {c: max(prev, carried.get(c, 0)) for c, prev in floors.items()}


def _inject_lost_ending(pump, reason: str) -> None:
    """The ``lost`` ending onto a pump a reap is about to abort: the viewer
    gets ``error`` then ``done``, the chat its ``turn_ended`` row. The
    producer's cancellation follows it on the event queue."""
    ending = turn_ending.TurnEnding(reason=turn_ending.LOST, detail=reason)
    pump.event_queue.put_nowait(CommonEvent(type=ERROR, data={
        "message": ending.line(), "ending": ending.as_dict()}))


def _encode_history(frame: dict, rows: list[dict]) -> str:
    """The history frame's text with ``rows`` as its ``messages``, one dumps
    per row (a worker thread encoding a long history yields the GIL between
    rows instead of holding it for one call)."""
    opts = {"separators": (",", ":"), "ensure_ascii": False}
    head = json.dumps(frame, **opts)
    body = ",".join(json.dumps(r, **opts) for r in rows)
    return head[:-1] + ',"messages":[' + body + "]}"


class ChatResumeMixin:
    """resume_chat: history replay, the visit path, restore and the lazy warmup"""

    # The live blocks each live pump held when the last resume took its cut,
    # {chat_id: (pump, blocks)}: the next attach to that pump replays the
    # ones a save persisted since (``_stream_via_pump``). One-shot.
    _resume_live: dict | None = None

    async def _queue_snapshot(self, pump=None) -> dict:
        """The QUEUE_SNAPSHOT frame for the viewed chat: every message
        waiting in its queue (``core/events/input_queue.py``), whoever typed
        it; on a task chat viewed through a sibling run's pump, that pump's
        chat's queue."""
        cid = pump.chat_id if pump is not None else self.chat_id
        q = await input_queue.loaded(cid)
        return {"type": wire.QUEUE_SNAPSHOT, "chat_id": self.chat_id,
                "messages": q.snapshot()}

    def _viewer_context(self) -> UserContext:
        """The connection's viewer as the principal the REST rules judge:
        the same role snapshot every other gate on this socket reads
        (refreshed by the authz re-check), so a by-id open answers exactly
        what the detail route would."""
        return UserContext(
            sub=self.user_sub, email=self.user.get("email") or "",
            name=self.user.get("name") or "", role=self.user_role,
            agents=list(self.user_agents), agent_roles=dict(self.agent_roles),
        )

    async def _handle_resume_chat(self, msg: dict):
        self.promised_pump_chat = None  # every resume resets the previous promise
        self._resume_live = None
        self._detach_pty_viewer()  # leaving any interactive chat we were viewing

        cid = msg.get("chat_id", "")
        if not cid:
            await self._send_error("chat_id required")
            return

        chat = await run_db(task_store.get_chat, cid)
        if not chat:
            await self._send_error("Chat not found")
            return
        # Imported here: the agents API package imports this module's assembly.
        from api.agents.chats import can_access_chat
        if not await run_db(can_access_chat, self._viewer_context(), chat):
            await self._send_error("Access denied")
            return
        # A chat this user may open but not drive (a Shared-only chat below
        # the editor tier, a task run they cannot continue): this connection
        # watches it and is never its notification sink or its time zone.
        refusal = await self._deny_task_continue(cid, chat, quiet=True)
        self._view_only = bool(refusal)
        # The sentence a chat that runs as the agent refuses this person with:
        # the dashboard locks its mode, model and terminal pickers on it. A
        # task chat keeps its own gate and locks nothing here.
        drive_refusal = (refusal if _vis.is_shared_chat_owner(chat.get("user_sub"))
                         and not session_kind.is_task_chat_id(cid) else "")
        locked = {"drive_refusal": drive_refusal} if drive_refusal else {}

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
                "type": wire.CHAT_HISTORY,
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
                **locked,
            })
            self._history_floors = None
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
        # A pick made in the new-chat state (deferred for the chat that
        # warmup would mint), or for another chat, must not follow this
        # socket into this chat's re-warm; one made for this chat while its
        # session was away waits for its next start.
        if self.deferred_for != cid:
            self.deferred_model = ""
            self.deferred_mode = ""
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
        is_task_chat = session_kind.is_task_chat_id(cid)

        # Multi-turn task runs: each turn has its own chat_id (task-{runId})
        # on one session_id, and the siblings' rows ride this view. Their
        # pumps are looked up BEFORE the rows are read: a pump that ends in
        # between drained its rows before it deregistered, and its cutoff
        # then keeps them all.
        related_chats: list[str] = []
        if is_task_chat:
            run_id = session_kind.run_id_of_chat(cid)

            def _related_chats_job() -> list[str]:
                run = task_store.get_run(run_id)
                if not run or not run.get("session_id"):
                    return []
                related = task_store.list_runs(
                    limit=50, session_id=run["session_id"],
                )
                related.sort(key=lambda r: r.get("started_at") or "")
                return [r["chat_id"] for r in related
                        if r["id"] != run_id and r.get("chat_id") and r["chat_id"] != cid]

            related_chats = await run_db(_related_chats_job)
        related_pumps = [_active_pumps.get(c) for c in related_chats]
        # A pump writing a steer's or a drained batch's rows: its blocks so
        # far are saved and the rows follow on its lane. The lane is drained
        # first, so the cut below never shows the rows above the blocks
        # they follow.
        for p in (_active_pumps.get(cid), *related_pumps):
            if p is not None and not p.is_done and getattr(p, "accepts_in_flight", 0):
                await chat_writer.drain(p.chat_id, timeout=2.0)
        # Each live pump's cutoff and live blocks, taken together before any
        # read (no await in between). A save landing during the reads raises
        # the cutoff and trims the blocks it persisted: the cut below uses the
        # cutoff of this instant, and the attach replays the blocks persisted
        # since, so a row the reads missed is in the live state instead. A
        # pump whose start job has not landed (cutoff None) is not held.
        held: dict[str, tuple] = {}
        for p in (_active_pumps.get(cid), *related_pumps):
            if p is not None and not p.is_done and p._db_msg_cutoff_id is not None:
                live = _chat_streaming_state.get(p.chat_id) or {}
                held[p.chat_id] = (p, p._db_msg_cutoff_id, list(live.get("live_blocks") or ()))

        def _cut_of(p) -> int | None:
            h = held.get(p.chat_id)
            return h[1] if h is not None and h[0] is p else p._db_msg_cutoff_id

        # A delta carries only the rows above the floors of the last history
        # this connection got for THIS chat: when the client takes deltas and
        # asked for one (its post-done refetch, `delta`) or the server
        # re-sends after a turn (`_delta`). Never on a resync, never without
        # a base: a chat the floors do not name is sent in full.
        prev_floors = (self._history_floors[1]
                       if self._history_floors and self._history_floors[0] == cid else None)
        as_delta = bool(
            self._history_deltas and prev_floors is not None and not msg.get("_resync")
            and (msg.get("delta") or msg.get("_delta")))
        read_floors = {c: int((prev_floors or {}).get(c, 0)) if as_delta else 0
                       for c in (cid, *related_chats)}
        if as_delta:
            # A sibling's row with a lower id can commit after a higher one
            # was observed: its lane is drained before the read.
            for c in related_chats:
                await chat_writer.drain(c, timeout=2.0)

            def _delta_job():
                return (task_store.get_chat_messages_since(read_floors),
                        task_store.get_chat_plans(cid), _build_chat_restore(cid))

            rows, plans, restore = await chat_writer.submit(
                cid, _delta_job, label="history_delta",
            )
            has_more = False
        else:
            def _history_job():
                # A chat-lane job: the page read lands BEHIND the pump's queued
                # saves for this chat, so a resume right after a turn sees its
                # rows; the plans + panel-restore state ride the same job.
                if is_task_chat:
                    msgs, more = task_store.get_chat_messages(cid), False
                else:
                    msgs, more = task_store.get_chat_messages_page(cid, _CHAT_PAGE)
                return msgs, more, task_store.get_chat_plans(cid), _build_chat_restore(cid)

            rows, has_more, plans, restore = await chat_writer.submit(
                cid, _history_job, label="history_read",
            )
            if related_chats:
                for extra in await run_db(
                        lambda: [task_store.get_chat_messages(c) for c in related_chats]):
                    rows.extend(extra)
        # A live sibling turn: its in-flight tail is withheld below (the
        # live_state replays it), and it is the pump this view attaches to
        # when the primary chat has none.
        active_task_pump = next((rp for rp in related_pumps if rp and not rp.is_done), None)

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
        # A resync (a viewer that fell a full queue behind) never reaps: its
        # pump is busy, not wedged.
        _wedged = None if msg.get("_resync") else _active_pumps.get(cid)
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
            # A prompt waiting on a person is silence by design (its own
            # wait bounds it), never a wedge.
            from core.session.session_state import has_pending_prompt
            _hard_stale = (_idle is not None and _idle > config.CLAUDE_TIMEOUT
                           and not has_pending_prompt(_wedged.session_id))
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
                if _unusable and is_task_chat:
                    from services.scheduler import scheduler as _sched
                    _rid = session_kind.run_id_of_chat(cid)
                    _full = f"reaped by platform: {_reason}"
                    if not _sched.platform_cancel_run(_rid, _full):
                        def _fail_run() -> None:
                            _run = task_store.get_run(_rid)
                            if _run and _run.get("status") == run_status.RUNNING:
                                task_store.update_run(
                                    _rid, status=run_status.FAILED, error_message=_full,
                                    completed_at=datetime.now(timezone.utc).isoformat(),
                                )
                        await run_db(_fail_run)
                _inject_lost_ending(_wedged, _reason)
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
        # transcript read-only instead (no truncation, no attach below). A
        # phone conversation is the call's between its turns too: this socket
        # never binds the call's session (the drive gate refuses every frame
        # on it, and a bound session would still take its viewer's slot).
        view_only_external = (
            session_kind.of_chat(chat) is session_kind.PHONE
            or _vis.is_phone_chat_owner(chat.get("user_sub"))
            or bool(pump and not pump.is_done
                    and pump.source_type in session_kind.EXTERNAL_DRIVEN_SOURCE_TYPES)
        )
        # The rows a live pump still streams are withheld by id (the live
        # stream re-sends them; a count slice breaks under paging). USER rows
        # are exempt: a mid-turn STEERED message (persisted the moment the
        # daemon accepts it, id ABOVE the cutoff) is NOT part of the pump's
        # replayed blocks, so filtering it made the message vanish when the
        # user switched away and back mid-turn. A cutoff of None (the start
        # job pending: no row of that turn exists yet) withholds nothing.
        cuts: dict[str, int | None] = {}
        if pump and not pump.is_done and not view_only_external:
            cuts[cid] = _cut_of(pump)
        elif active_task_pump:
            # Related turn has active pump — primary messages are complete
            pump = active_task_pump
        for rp in related_pumps:
            if rp and not rp.is_done:
                cuts[rp.chat_id] = _cut_of(rp)
        self._resume_live = {c: (h[0], h[2]) for c, h in held.items()}
        messages, new_floors = _cut_rows(rows, cuts, read_floors)
        if is_task_chat:
            # The turns as they happened: a reload and a delta converge on
            # one order, whichever run a row belongs to.
            messages.sort(key=lambda m: int(m["id"]))

        frame = {"type": wire.CHAT_HISTORY_DELTA if as_delta else wire.CHAT_HISTORY,
                 "chat_id": cid,
                 # Agent of record, from the chat row — the frontend uses it
                 # to normalize a mismatched /chat/:name/:chatId URL (a
                 # deep-link/redirect can carry the wrong slug; the route
                 # param is otherwise trusted for the UI shell).
                 "agent": chat["agent"],
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
                     or await task_run_active_async(chat["id"])
                     or await chat_process_alive(chat)
                 ),
                 **locked}
        # The attach that follows (below: a live pump that is neither done
        # nor an external session's) sends the turn's live_state: the client
        # holds these rows and paints both at once.
        if pump and not pump.is_done and not view_only_external:
            frame["live_pending"] = True
        if as_delta:
            frame["since_id"] = min(read_floors.values())
        # Encoded off the loop, row by row: a long history's dump never holds
        # the loop, and the thread yields between rows.
        if await self._send_text(await asyncio.to_thread(_encode_history, frame, messages)):
            self._history_floors = (cid, new_floors)

        if view_only_external:
            # Read-only view of a live external session: history is sent, but we
            # do NOT acquire the session slot, send warmup_ready, or promise a
            # pump attach — leaving the phone/webhook driver's stream untouched
            # so the call keeps playing and tears down normally.
            kind = pump.source_type if pump else session_kind.of_chat(chat).source_type
            logger.info(
                f"WS dashboard: chat={cid} is driven by a {kind} session — "
                f"read-only view, not attaching"
            )
            return

        if pump and not pump.is_done:
            # Confirm the concurrency slot (idempotent for a tracked session;
            # the reconciler may have dropped one), using the chat's pinned
            # target so a REMOTE session stays off the local ceiling G.
            from core.concurrency import acquire_chat_slot
            adm = await acquire_chat_slot(
                pump.session_id, target=chat.get("execution_target") or placement.LOCAL,
                execution_path=chat.get("execution_path"),
                user_sub=None if self._view_only else self.user_sub,
                per_user_cap=False,
            )
            if not adm:
                await self._send_error(adm.user_message)
                return
            self.session_id = pump.session_id
            # Refresh session's user_tz from this user's last client_info —
            # handles the WS-reconnect case where session was originally
            # warmed up before any client_info had arrived.
            _user_default_tz = get_user_tz(self.user_sub)
            if _user_default_tz and not self._view_only:
                set_session_user_tz(self.session_id, _user_default_tz)
            await self._send({
                "type": wire.WARMUP_READY,
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
            # disconnect).
            await self._send(await self._queue_snapshot(pump))
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
                await acquire_chat_slot(old_session_id, target=isess.target,
                                        user_sub=None if self._view_only else self.user_sub,
                                        per_user_cap=False)
                self.session_id = old_session_id
                _user_default_tz = get_user_tz(self.user_sub)
                if _user_default_tz and not self._view_only:
                    set_session_user_tz(self.session_id, _user_default_tz)
                await self._send({
                    "type": wire.WARMUP_READY,
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
                await self._send(await self._queue_snapshot())
                # Client attaches the PTY viewer via pty_attach (see _dispatch).
                return
            # A machine in its reconnect grace is coming back: its session
            # stays the chat's (a turn sent meanwhile waits for it).
            if self.layer and (
                    await self.layer.is_session_alive(old_session_id)
                    or grace_layer(old_session_id, self.layer).is_session_grace_held(
                        old_session_id)):
                # Confirm the concurrency slot (idempotent for a tracked
                # session), using the chat's pinned target so a REMOTE session
                # stays off G.
                from core.concurrency import acquire_chat_slot
                adm = await acquire_chat_slot(
                    old_session_id, target=chat.get("execution_target") or placement.LOCAL,
                    execution_path=chat.get("execution_path"),
                    user_sub=None if self._view_only else self.user_sub,
                    per_user_cap=False,
                )
                if not adm:
                    await self._send_error(adm.user_message)
                    return
                self.session_id = old_session_id
                # Refresh session's user_tz from this user's last client_info.
                _user_default_tz = get_user_tz(self.user_sub)
                if _user_default_tz and not self._view_only:
                    set_session_user_tz(self.session_id, _user_default_tz)
                live_mode = get_session_mode(old_session_id) or perm_mode
                await self._send({
                    "type": wire.WARMUP_READY,
                    "session_id": self.session_id,
                    "chat_id": self.chat_id,
                    "mode": live_mode,
                    "model": chat_model,
                    "execution_path": effective_exec_path,
                    "interactive": False,
                    **(await run_db(self._target_mismatch_fields, self.chat_id)),
                })
                # No active pump: the chat's queue. Emit so the client
                # clears any reload-persisted stale entries.
                await self._send(await self._queue_snapshot())
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
                if evt_type == wire.ITEM_PERMISSION_PROMPT:
                    await chat_writer.submit(
                        self.chat_id,
                        functools.partial(
                            task_store.add_chat_message, self.chat_id, "event", "",
                            event_type=wire.PERMISSION_PROMPT,
                            event_data=json.dumps({
                                "type": wire.PERMISSION_PROMPT,
                                "request_id": stale_perm.get("request_id", ""),
                                "tool_name": stale_perm.get("tool_name", ""),
                                "tool_input": stale_perm.get("tool_input", {}),
                                "resolved": True, "approved": False,
                            }),
                        ),
                        label="stale_permission",
                    )
                    logger.info(f"WS dashboard: saved stale permission as rejected for chat={self.chat_id}")
                elif evt_type == wire.ITEM_PLAN_REVIEW:
                    # Keep plan_review pending — user can still implement after session resume
                    logger.info(f"WS dashboard: keeping stale plan_review pending for chat={self.chat_id}")
        self.session_id = None
        await self._send({
            "type": wire.WARMUP_READY,
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
        # finished but before sending again), keeping what the chat's
        # queue holds.
        await self._send(await self._queue_snapshot())
        logger.info(
            f"WS dashboard resume_chat (lazy): session dead/missing, "
            f"chat={self.chat_id}, agent={self.agent_name} — will warmup on first message"
        )
