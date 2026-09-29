"""One remote turn: send, stream, settle, stop (mixin).

``_send_turn`` runs the shared part of a turn on a satellite — the upload
barrier, the time prefix, the abort-ack wait, the stale-event drain, the
command id, the ``send_message`` frame, the ``turn_active`` guard — and
streams the turn through the ENGINE's remote adapter
(``RemoteEngineAdapter.begin_turn`` / ``stream_turn``), which owns the
per-engine translation and turn-end decision. Nothing here names an
engine. Mixed into RemoteExecutionLayer; split out of remote_execution.py.
"""

import asyncio
import logging
import time
import uuid
from collections import Counter
from typing import AsyncIterator

from core.events.common_events import CommonEvent, DONE, ERROR
from core.remote import upload_inflight
from core.remote.remote_session_info import RemoteSessionInfo
from core.session.session_state import (
    get_session_user_tz,
    resolve_bg_command_frame,
)
import config

logger = logging.getLogger("remote-layer")


def record_resume_handle(info: RemoteSessionInfo, handle: str) -> None:
    """The satellite reported the engine's resume handle for ``info``'s
    session (the ``_resume_handle`` queue marker): record it on the session.
    An engine's remote adapter calls this when its queue consumer sees the
    marker.

    Deliberately NOT a database write. The marker lands right behind
    ``session_started`` — before the dashboard warmup has bound the chat to
    the session — so a ``get_chat_by_session`` here finds nothing (the
    released code did exactly that and every remote Codex chat since the
    router started at session start lost its thread id). The chat row
    (``chats.codex_thread_id``) is written by the stream pump from the
    METADATA event the adapter emits on the first turn — the same path the
    local layer uses."""
    if handle and handle != info.resume_handle:
        info.resume_handle = handle


class RemoteTurnMixin:
    # --- ExecutionLayer interface: one turn ---

    def engine_for(self, session_id: str) -> str:
        """The engine behind a remote session, for ``session_events`` — read
        from that engine's own descriptor. "" for an unknown session (this
        runs before the session is checked)."""
        info = self._sessions.get(session_id)
        if info is None:
            return ""
        return self.capabilities_for(session_id).identity.short_name

    async def _send_turn(
        self, session_id: str, message: str, **kwargs,
    ) -> AsyncIterator[CommonEvent]:
        """One turn on the satellite (``send_message`` on the base wraps the
        turn-end loop)."""
        info = self._sessions.get(session_id)
        if not info:
            yield CommonEvent(type=ERROR, data={"message": "Remote session not found"})
            return
        adapter = self._adapter(info)

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

        if not adapter.owns_event_queue:
            # Drain any stale events left over from a previous aborted turn —
            # analog of PersistentSession._drain_stale_output on local. (An
            # engine whose router owns the queue has no stragglers here: a
            # prior turn's leftovers went to a bg buffer or the now-discarded
            # prior consumer.)
            await self._drain_stale_events(info)

        # Fresh per-turn state — the engine's (a per-turn translator and settle
        # controller, the registry prunes, a fresh main-turn consumer).
        await adapter.begin_turn(info, settle_after_result)

        # Pre-mint the command_id so we can correlate the satellite's
        # eventual ``turn_ended`` back to THIS send_message. A previous turn's
        # late turn_ended (delayed by the satellite-side file-scan running
        # past our 2s drain budget) carries the prior command_id and is
        # filtered out by the engine's turn stream. Without this, a stale
        # turn_ended terminates the new turn with zero events and the agent's
        # response is never persisted.
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
            # verifies the transcript before issuing --resume, so a truly
            # unresumable chat reseeds from history rather than resuming blind.
            err_text = str(e)
            if "CLI process not running" in err_text or "Session not found" in err_text:
                info.cli_dead = True
            yield CommonEvent(type=ERROR, data={"message": err_text})
            yield CommonEvent(type=DONE)
            return

        # Read events and translate through the engine's turn stream, which
        # owns the turn-end decision. ``turn_active`` guards the whole stream
        # (cleared even when the generator is abandoned) so the idle reaper
        # can tell an in-flight turn from a genuinely idle session.
        info.turn_active = True
        try:
            async for event in adapter.stream_turn(info, self._cm):
                yield event
        finally:
            info.turn_active = False

    async def _drain_stale_events(self, info: RemoteSessionInfo) -> None:
        """Drain frames buffered on ``info.event_queue`` between turns. Log a
        bounded type census (never payloads — frames can carry base64
        bodies): a swallowed late answer from an orphaned prior turn is
        otherwise invisible (queue events buffered satellite-side between
        turns arrive AFTER this drain and are handled by the engine's
        foreign-skip gate instead). A self-wake bracket buffered here (the
        satellite's post-turn drain forwarded it while nobody listened) is
        CAPTURED to the chat instead of dropped — the pre-send rule that
        makes the next bracket the user turn's own."""
        from core.events import wake_capture as _wake
        from core.remote.remote_bg_commands import queue_frame_reader
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
                    info.session_id, ev, queue_frame_reader(info),
                    source="remote_stale_drain",
                )
                continue
            if isinstance(ev, dict):
                resolve_bg_command_frame(info.session_id, ev)
                _reconcile_snapshot(info.session_id, ev)
            drained[(ev or {}).get("type", "?") if isinstance(ev, dict) else "?"] += 1
        if drained:
            logger.warning(
                "Remote session %s: drained %d stale events (types=%s, results=%d)",
                info.session_id[:8], sum(drained.values()), dict(drained),
                drained.get("result", 0),
            )
