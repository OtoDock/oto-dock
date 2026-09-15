"""One remote turn: send, stream, settle, stop (mixin).

``send_message`` and the two per-engine streams over the satellite's
forwarded event queue: the CLI turn with the shared translator and settle
controller, the Codex turn with the shared Codex translator, the stale-turn
filters keyed on the send command id, ``stop_turn`` and the post-stop
drain. Mixed into RemoteExecutionLayer; split out of remote_execution.py.
"""

import asyncio
import logging
import time
import uuid
from collections import Counter
from typing import AsyncIterator

from core.events.common_events import CommonEvent, DONE, ERROR, METADATA, PLAN_MODE
from core.layers.cli.layer import cli_chunk_to_events
from core.layers.cli.translator import ClaudeCLIEventTranslator
from core.layers.cli.settle import (
    FOREIGN_SKIP_SILENCE_S,
    ForeignSkipGate,
    SettleController,
    chunk_is_content,
    is_foreign_result,
)
from core.layers.codex.session import CodexEvent
from core.remote import upload_inflight
from core.remote.remote_session_info import RemoteSessionInfo
from core.session.session_state import (
    get_session_user_tz,
    reset_subagent_registry,
    resolve_bg_command_frame,
)
from core.events.bg_command_state import reset_bg_command_registry
import config

logger = logging.getLogger("remote-layer")


def _record_rate_limit(session_id: str, info: dict) -> None:
    """A ``rate_limit`` chunk: the account's window state, for the pool."""
    from services.engines import subscription_windows as _sw
    _sw.record_claude_event_async(session_id, info)


class RemoteTurnMixin:
    # --- ExecutionLayer interface: one turn ---

    async def send_message(
        self, session_id: str, message: str, **kwargs,
    ) -> AsyncIterator[CommonEvent]:
        info = self._sessions.get(session_id)
        if not info:
            yield CommonEvent(type=ERROR, data={"message": "Remote session not found"})
            return

        info.last_activity = time.monotonic()

        # Barrier on in-flight dashboard-upload pushes for this agent:
        # ``/v1/upload`` returns before its satellite push completes
        # (``api/media/uploads.py``), so a prompt referencing a just-attached
        # file must wait for the bytes to land on the machine first. Bounded —
        # a wedged push logs and the turn proceeds (the pre-existing
        # best-effort failure mode). Returns immediately (no wait) when
        # nothing is in flight, i.e. the vast majority of turns.
        await upload_inflight.wait_settled(info.agent_name)

        inject_time = kwargs.get("inject_time", False)
        settle_after_result = kwargs.get("settle_after_result", 0)

        # Inject the time prefix on the proxy side so the satellite doesn't
        # need to know the user's TZ. Satellite is told inject_time=False —
        # we pre-inject here using the session's user_tz (or platform fallback).
        if inject_time:
            user_tz = get_session_user_tz(session_id)
            message = f"[Current time: {config.format_current_time(user_tz)}]\n\n{message}"
            inject_time = False

        # If this turn follows an abort, wait for the satellite's
        # `session_aborted` confirmation first so the dying CLI's flushed output
        # has fully arrived before we drain it (otherwise late stale events —
        # e.g. a buffered image — leak into this turn, and we'd race the
        # still-dying subprocess on resume). No-op for normal turns: no abort
        # event is armed, so this returns immediately.
        acked = await self._cm.wait_abort_acked(info.machine_id, session_id)
        if not acked and info.cli_dead:
            logger.warning(
                "Remote session %s: resume proceeding without abort ack "
                "(old satellite or lost message)", session_id[:8],
            )

        if info.bg_supervised:
            # The router owns info.event_queue and demuxes by thread, so there are
            # no main-thread stragglers to drain here (a prior turn's leftovers
            # either went to a bg buffer or the now-discarded prior consumer).
            # Register a fresh main-turn consumer for the router to feed.
            info.default_consumer = asyncio.Queue()
        else:
            # Drain any stale events left over from a previous aborted turn —
            # analog of PersistentSession._drain_stale_output on local. Log a
            # bounded type census (never payloads — frames can carry base64
            # bodies): a swallowed late answer from an orphaned prior turn is
            # otherwise invisible (queue events buffered satellite-side
            # between turns arrive AFTER this drain and are handled by the
            # foreign-skip gate instead). 1.5: a self-wake bracket buffered
            # here (satellite post-turn drain forwarded it while nobody
            # listened) is CAPTURED to the chat instead of dropped — the
            # pre-send rule that makes the next bracket the user turn's own.
            from core.events import wake_capture as _wake
            from core.session.session_state import (
                reconcile_background_snapshot as _reconcile_snapshot,
            )
            drained: Counter[str] = Counter()
            while True:
                try:
                    ev = info.event_queue.get_nowait()
                except asyncio.QueueEmpty:
                    break
                if isinstance(ev, dict) and _wake.is_wake_init(ev):
                    drained["wake_captured"] += 1
                    await _wake.capture_wake_turn(
                        session_id, ev, self._make_queue_frame_reader(info),
                        source="remote_stale_drain",
                    )
                    continue
                if isinstance(ev, dict):
                    resolve_bg_command_frame(session_id, ev)
                    _reconcile_snapshot(session_id, ev)
                drained[(ev or {}).get("type", "?") if isinstance(ev, dict) else "?"] += 1
            if drained:
                logger.warning(
                    "Remote session %s: drained %d stale events (types=%s, results=%d)",
                    session_id[:8], sum(drained.values()), dict(drained),
                    drained.get("result", 0),
                )

        # Fresh per-turn state for CLI. Codex translator persists across turns
        # (cumulative token tracking lives in it).
        if info.execution_path == "claude-code-cli":
            reset_subagent_registry(session_id)
            reset_bg_command_registry(session_id)
            info.cli_translator = ClaudeCLIEventTranslator(session_id)
            info.cli_settle = SettleController(
                session_id, settle_after_result, info.cli_translator,
            )
        else:
            # Codex: per-turn bg-command registry prune (mirrors the LOCAL
            # session's send_message). reset() PRESERVES still-pending
            # terminals; without it `unsurfaced` never clears remotely and
            # task_producer's cohort math fires spurious review turns.
            reset_bg_command_registry(session_id)

        # Pre-mint the command_id so we can correlate the satellite's
        # eventual ``turn_ended`` back to THIS send_message. A previous turn's
        # late turn_ended (delayed by the satellite-side file-scan running
        # past our 2s drain budget) carries the prior command_id and is
        # filtered out by _stream_cli_turn / _stream_codex_turn / _drain_*.
        # Without this, a stale turn_ended terminates the new turn with zero
        # events and the agent's response is never persisted.
        command_id = str(uuid.uuid4())
        info.current_send_command_id = command_id

        # Send message to satellite
        try:
            await self._cm.send_command(info.machine_id, {
                "type": "send_message",
                "session_id": session_id,
                "message": message,
                "execution_path": info.execution_path,
                "inject_time": inject_time,
            }, command_id=command_id)
        except Exception as e:
            # If the satellite reports there's no live session to take the
            # message, flag it so the dashboard's auto-resume path spawns a
            # fresh one (resumed from the on-disk transcript) on the next
            # attempt. Two shapes mean the same "no session on the satellite":
            #   - "CLI process not running": the session object survives but
            #     its subprocess died (crash / prior abort) — see
            #     session_manager.send_message.
            #   - "Session not found": the session object itself is gone from
            #     the satellite registry — the dominant case after a satellite
            #     WS drop/reconnect kills and drops in-flight sessions while
            #     the proxy still holds a stale, connected-looking info (so
            #     is_session_process_dead read False and let the send through).
            # Without mapping the latter to cli_dead the chat was bricked:
            # every resend hit the same dead id and re-raised "Session not
            # found", never triggering auto-resume. can_resume_session still
            # RPC-verifies the transcript before issuing --resume, so a truly
            # unresumable chat reseeds from history rather than resuming blind.
            err_text = str(e)
            if "CLI process not running" in err_text or "Session not found" in err_text:
                info.cli_dead = True
            yield CommonEvent(type=ERROR, data={"message": err_text})
            yield CommonEvent(type=DONE)
            return

        # Read events and translate. For CLI, SettleController decides the
        # turn-end timing (mirrors local PersistentSession.send_message).
        # For Codex, the Codex translator handles its own turn lifecycle.
        # ``turn_active`` guards the whole stream (cleared even when the
        # generator is abandoned) so the idle reaper can tell an in-flight
        # turn from a genuinely idle session.
        info.turn_active = True
        try:
            if info.execution_path == "claude-code-cli":
                async for event in self._stream_cli_turn(info):
                    yield event
            else:
                try:
                    async for event in self._stream_codex_turn(info):
                        yield event
                finally:
                    # At main-turn end, hand any still-running background
                    # sub-agents off to per-thread supervisors (mirrors the
                    # LOCAL session's send_message finally). No-op when bg
                    # supervision is off.
                    if info.bg_supervised:
                        self._handoff_remote_bg_subagents(info)
        finally:
            info.turn_active = False

    # --- Per-layer turn streaming ---

    async def _stream_cli_turn(
        self, info: "RemoteSessionInfo",
    ) -> AsyncIterator[CommonEvent]:
        """Stream one CLI turn to the pump using the shared translator + settle.

        Satellite is a dumb pipe: it forwards every NDJSON line as a
        session_event. This method owns turn-end decisions and sends
        ``stop_turn`` to the satellite when the turn is over.
        """
        translator = info.cli_translator
        settle = info.cli_settle
        assert translator and settle  # invariant: set in send_message

        expected_cmd_id = info.current_send_command_id
        # Foreign-result skip regime (see settle.ForeignSkipGate): resume
        # handshake / settlement results must not close the driven turn —
        # the valve, not a count, ends the regime.
        gate = ForeignSkipGate(info.session_id, translator)
        while True:
            timeout = gate.clamp_timeout(settle.effective_timeout())
            try:
                raw = await asyncio.wait_for(
                    info.event_queue.get(), timeout=timeout,
                )
            except asyncio.TimeoutError:
                if gate.expired() and not settle.settling:
                    if self._cm.is_session_in_grace(
                        info.machine_id, info.session_id,
                    ):
                        # WS-drop grace: events are buffering satellite-side
                        # and Mode A may still deliver the turn — closing
                        # here would recreate the empty-turn/orphan pair.
                        # Bounded: grace expiry pushes error+None sentinels
                        # that close the turn regardless of the valve.
                        gate.re_arm()
                        continue
                    # Silence valve: nothing followed the skip regime — the
                    # mis-skipped result was probably legitimate after all
                    # (empty answer / idle CLI). Close the turn instead of
                    # hanging it forever.
                    logger.warning(
                        "Remote session %s: no events %.0fs after a skipped "
                        "foreign result — closing the turn",
                        info.session_id[:8], FOREIGN_SKIP_SILENCE_S,
                    )
                    await self._send_stop_turn(info)
                    await self._drain_until_turn_ended(
                        info, timeout=2.0,
                        expected_command_id=expected_cmd_id,
                    )
                    yield CommonEvent(type=DONE)
                    return
                if not settle.settling:
                    # Pre-settle: long silence, keep waiting.
                    continue
                settle.maybe_log_heartbeat(proc_alive=True)
                if settle.should_exit_on_silence(timeout):
                    await self._send_stop_turn(info)
                    # Wait briefly for satellite to drain + ack turn_ended
                    await self._drain_until_turn_ended(
                        info, timeout=2.0,
                        expected_command_id=expected_cmd_id,
                    )
                    yield CommonEvent(type=DONE)
                    return
                continue

            if raw is None:
                # Session ended on satellite (process exit) — terminal.
                yield CommonEvent(type=DONE)
                return

            # An event arrived — slide the post-skip valve (never disarm:
            # noise must not remove the skip regime's only closer).
            gate.note_event()

            rtype = raw.get("type", "")
            # Stale-turn filter: the satellite tags each CLI turn event with
            # the send_message command it was streamed under. A result/error
            # from a PREVIOUS turn (the replaced process's dying flush racing
            # past the send-start drain) must not terminate THIS turn.
            # Untagged events (older satellite) pass through; content-type
            # events pass regardless (a late bg task_updated frame from the
            # prior turn must still reach the registries).
            ev_cmd = raw.get("_command_id", "")
            if (ev_cmd and expected_cmd_id and ev_cmd != expected_cmd_id
                    and rtype in ("result", "error")):
                logger.warning(
                    "Remote session %s: dropping stale %s event from a "
                    "previous turn (expected=%s, got=%s)",
                    info.session_id[:8], rtype,
                    expected_cmd_id[:8], ev_cmd[:8],
                )
                continue
            if rtype == "error":
                yield CommonEvent(type=ERROR, data=raw)
                yield CommonEvent(type=DONE)
                return
            if rtype == "_turn_ended":
                # Filter stale turn_ended from a previous turn whose
                # satellite-side file-scan delivered late (past the proxy's
                # drain timeout). The matching command_id is set by
                # send_message before dispatch; an empty/non-matching id
                # means the marker is leftover and must be discarded so the
                # actual response of THIS turn can stream.
                ended_cmd_id = raw.get("command_id", "")
                if expected_cmd_id and ended_cmd_id and ended_cmd_id != expected_cmd_id:
                    logger.info(
                        "Remote session %s: discarding stale turn_ended "
                        "(expected=%s, got=%s)",
                        info.session_id[:8],
                        expected_cmd_id[:8], ended_cmd_id[:8],
                    )
                    continue
                yield CommonEvent(type=DONE)
                return

            # Feed the translator and forward all chunks. Content counts
            # only from events streamed under THIS command (untagged passes
            # for old satellites): content-typed stale events deliberately
            # bypass the stale filter above, and a dying prior process's
            # junk text must not flip the next zero-content result to
            # "real" and close the turn on it.
            info.last_activity = time.monotonic()
            # An init DURING settle = the CLI's self-wake review turn running
            # INLINE through this stream (its content joins the task output)
            # — note it so the producer/monitors skip the redundant nudge.
            # Twin of the local session loop's marker.
            if (settle.settling and rtype == "system"
                    and raw.get("subtype") == "init"):
                from core.events import wake_capture as _wake
                _wake.note_captured(info.session_id)
            for chunk in translator.feed(raw):
                if chunk.event_type == "rate_limit":
                    # The account's window state: for the pool, never the chat.
                    _record_rate_limit(info.session_id, chunk.event_data)
                    continue
                if (chunk_is_content(chunk)
                        and (not ev_cmd or ev_cmd == expected_cmd_id)):
                    gate.note_content()
                for event in cli_chunk_to_events(chunk):
                    yield event

            # Result-event book-keeping — turn-end logic. A foreign result
            # (resume handshake / zero-content settlement mini-turn) never
            # ends the turn in EITHER mode: interactive must not close on
            # it, and task mode must not enter settle on it (the job-done
            # 5s fast-grace would close the turn just the same).
            if rtype == "result":
                if not settle.settling and is_foreign_result(
                    raw, gate.content_since_skip,
                ):
                    gate.record_skip(raw)
                    continue
                if settle.is_interactive_done():
                    # Interactive chat: no settle — tell satellite to stop.
                    await self._send_stop_turn(info)
                    await self._drain_until_turn_ended(
                        info, timeout=2.0,
                        expected_command_id=expected_cmd_id,
                    )
                    yield CommonEvent(type=DONE)
                    return
                # Task settle mode: translator resets parsing state, keeps counters.
                settle.enter_settle()

    async def _stream_codex_turn(
        self, info: "RemoteSessionInfo",
    ) -> AsyncIterator[CommonEvent]:
        """Stream one Codex turn to the pump using the shared translator.

        When bg supervision is on (info.bg_supervised), the per-session router
        owns info.event_queue and feeds this turn's MAIN-thread events to
        info.default_consumer (bg sub-agent threads go to per-thread buffers);
        otherwise we read event_queue directly (today's behavior)."""
        expected_cmd_id = info.current_send_command_id
        turn_start = time.monotonic()
        # Plan mode: mirror the local layer's synthetic implement-card. Codex
        # delivers the plan as the turn's final agentMessage (no ExitPlanMode on
        # the -p path), so a completed plan-mode turn synthesizes a `plan_mode
        # exit` right before DONE — the SAME event the local codex layer emits,
        # rendered by the same PlanView card. `info.mode == "plan"` is the
        # read-only plan signal (set by change_mode / at session start).
        in_plan = info.mode == "plan"
        final_plan_msg = ""
        turn_interrupted = False

        def _plan_card() -> "CommonEvent | None":
            if in_plan and not turn_interrupted and final_plan_msg.strip():
                return CommonEvent(type=PLAN_MODE, data={
                    "action": "exit", "synthetic": True,
                    "tool_input": {"plan": final_plan_msg},
                })
            return None

        q = (
            info.default_consumer
            if (info.bg_supervised and info.default_consumer is not None)
            else info.event_queue
        )
        while True:
            try:
                raw = await asyncio.wait_for(q.get(), timeout=300.0)
            except asyncio.TimeoutError:
                # Tell the satellite to kill the orphaned codex turn process
                # (the CLI path sends stop_turn on its own timeout; codex had
                # no equivalent, leaking the process + its late output into the
                # next turn). The satellite's killpg now targets only the turn
                # group thanks to start_new_session.
                try:
                    await self._cm.send_fire_and_forget(info.machine_id, {
                        "type": "abort", "session_id": info.session_id,
                    })
                except Exception:
                    logger.warning(
                        "codex turn-timeout abort send failed for %s",
                        info.session_id[:8],
                    )
                yield CommonEvent(type=ERROR, data={"message": "Remote session timeout"})
                yield CommonEvent(type=DONE)
                return

            if raw is None:
                yield CommonEvent(type=DONE)
                return

            rtype = raw.get("type", "")
            if rtype == "error":
                yield CommonEvent(type=ERROR, data=raw)
                yield CommonEvent(type=DONE)
                return
            if rtype == "_turn_ended":
                # See _stream_cli_turn for the same stale-turn filter rationale.
                ended_cmd_id = raw.get("command_id", "")
                if expected_cmd_id and ended_cmd_id and ended_cmd_id != expected_cmd_id:
                    logger.info(
                        "Remote session %s: discarding stale turn_ended "
                        "(codex, expected=%s, got=%s)",
                        info.session_id[:8],
                        expected_cmd_id[:8], ended_cmd_id[:8],
                    )
                    continue
                card = _plan_card()
                if card:
                    yield card
                yield CommonEvent(type=DONE)
                return
            if rtype == "_codex_thread_id":
                info.codex_thread_id = raw.get("thread_id", "")
                from storage.database import update_chat, get_chat_by_session
                chat = get_chat_by_session(info.session_id)
                if chat:
                    update_chat(chat["id"], codex_thread_id=info.codex_thread_id)
                continue

            # Plan-mode capture (mirrors codex/layer.py): the last agentMessage
            # is the plan; a `turn/completed` interrupted status suppresses the
            # card. Read the raw notification before translation.
            if in_plan:
                _method = raw.get("method", "")
                if _method == "item/completed":
                    _item = (raw.get("params") or {}).get("item") or {}
                    if _item.get("type") == "agentMessage" and _item.get("text"):
                        final_plan_msg = _item["text"]
                elif _method == "turn/completed":
                    if ((raw.get("params") or {}).get("turn") or {}).get("status") == "interrupted":
                        turn_interrupted = True

            info.last_activity = time.monotonic()
            for event in self._translate_codex_event(info, raw):
                # Codex doesn't report turn duration — measure it proxy-side
                # and stamp it onto the METADATA event (mirrors codex/layer.py;
                # remote Codex turns were persisting duration_ms=0).
                if event.type == METADATA and "cost_usd" in event.data:
                    event.data["duration_ms"] = int(
                        (time.monotonic() - turn_start) * 1000
                    )
                if event.type == DONE:
                    card = _plan_card()
                    if card:
                        yield card
                yield event
                if event.type == DONE:
                    return

    async def _send_stop_turn(self, info: "RemoteSessionInfo") -> None:
        """Tell the satellite to exit its stdout read loop for this turn. If
        background work is still pending, ask the satellite to keep draining
        + forwarding stdout post-turn (``drain_bg``) so the completion frames
        — which claude emits idle, AFTER this turn ends — still reach the
        proxy (the satellite's per-turn forwarder otherwise stops here,
        stranding the badge + skipping the autonudge). 1.5: armed for
        pending SUBAGENTS too — their completion also triggers the CLI's
        self-wake turn on stdout, which the proxy-side drain captures; the
        satellite's drain loop forwards every frame unfiltered (verified
        0.5.110 ``_bg_drain_loop``), so no satellite change/gate is needed."""
        from core.events.bg_command_state import get_bg_command_registry
        from core.session.session_state import get_subagent_registry
        drain_bg = (get_bg_command_registry(info.session_id).has_pending
                    or get_subagent_registry(info.session_id).has_pending)
        try:
            await self._cm.send_fire_and_forget(info.machine_id, {
                "type": "stop_turn",
                "session_id": info.session_id,
                "drain_bg": drain_bg,
            })
        except Exception as e:
            logger.warning(
                "Remote stop_turn send failed for session %s: %s",
                info.session_id[:8], e,
            )

    async def _drain_until_turn_ended(
        self, info: "RemoteSessionInfo", *, timeout: float,
        expected_command_id: str = "",
    ) -> None:
        """After sending stop_turn, drain any remaining queue events until
        ``_turn_ended`` arrives or ``timeout`` expires.

        Prevents orphaned events from leaking into the next turn's queue
        and gives the satellite time to deliver any final in-flight events.

        When ``expected_command_id`` is provided, only a turn_ended carrying
        the same command_id terminates the drain — stale turn_ended markers
        from a PRIOR turn (whose late file-scan delivered after that turn's
        own drain budget) are silently discarded so they can't be confused
        with this turn's end.
        """
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            remaining = max(0.05, deadline - time.monotonic())
            try:
                raw = await asyncio.wait_for(info.event_queue.get(), timeout=remaining)
            except asyncio.TimeoutError:
                return
            if raw is None:
                return
            if isinstance(raw, dict) and raw.get("type") == "_turn_ended":
                ended_cmd_id = raw.get("command_id", "")
                if expected_command_id and ended_cmd_id and ended_cmd_id != expected_command_id:
                    # Stale marker from a previous turn — keep draining.
                    continue
                return

    # --- Event translators ---

    def _translate_codex_event(
        self, info: RemoteSessionInfo, raw_event: dict,
    ) -> list[CommonEvent]:
        """Translate a forwarded Codex app-server notification to CommonEvents.

        The satellite forwards each ``codex app-server`` notification verbatim as
        ``{"method": ..., "params": ...}`` (the same NDJSON the local layer sees);
        the shared :class:`CodexEventTranslator` keys on the method. CLI events go
        through ``ClaudeCLIEventTranslator + cli_chunk_to_events`` in
        ``_stream_cli_turn`` — there is no equivalent helper here.
        """
        if not info.codex_translator:
            return []

        # Seed the multi-agent demux with the AUTHORITATIVE main thread id (the
        # satellite-reported `_codex_thread_id`) so a spawned sub-agent's thread
        # can't hijack `_main_thread_id` and suppress the MAIN agent's output.
        # Idempotent; mirrors the local layer + the satellite session-loop guard.
        if info.codex_thread_id:
            info.codex_translator._main_thread_id = info.codex_thread_id

        method = raw_event.get("method", "")
        codex_event = CodexEvent(type=method, data=raw_event.get("params") or {})
        return info.codex_translator.translate(codex_event)
