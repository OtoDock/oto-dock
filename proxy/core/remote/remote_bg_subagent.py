"""Remote Codex background sub-agent supervision (mixin).

Proxy-side mirror of core/layers/codex/session.py's router/supervisor: when a
remote Codex agent runs background sub-agents (version-gated, info.bg_supervised),
this consumes the WS-forwarded session_event stream, demuxes per-thread, marks
the shared SubagentRegistry, and tears down on close. Mixed into
RemoteExecutionLayer; split out of remote_execution.py.
"""

import asyncio
import contextlib
import logging
import time
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from core.remote.remote_execution import RemoteSessionInfo  # noqa: F401 (quoted annotations)

logger = logging.getLogger("remote-layer")


class RemoteBgSubagentMixin:
    # ------------------------------------------------------------------
    # Remote Codex background sub-agent demux — router + supervisors.
    # Proxy-side mirror of core/layers/codex/session.py's router/supervisor,
    # reading the WS-forwarded session_event stream instead of the daemon's
    # notif_queue. Active only when info.bg_supervised (version-gated). Reuses
    # the shared SubagentRegistry + resolve_bg_subagent + _bg_agent_monitor +
    # wait_for_bg_subagents, so a delegated REMOTE Codex agent waits for its bg
    # subs exactly like a local one.
    # ------------------------------------------------------------------

    async def _route_remote_notifications(self, info: "RemoteSessionInfo") -> None:
        """Sole consumer of info.event_queue when bg supervision is on.

        Demultiplexes by ``threadId``: MAIN-thread codex events (plus the
        synthetic turn-control markers _turn_ended / _codex_thread_id / a
        satellite-level error, which carry no codex ``method``) go to the active
        turn's ``default_consumer``; each spawned sub-agent thread's events go to
        its own buffer so a background sub keeps streaming to the proxy after the
        main turn ends. Runs for the session's lifetime; cancelled by
        _teardown_remote_bg. Mirrors the LOCAL _route_notifications."""
        q = info.event_queue
        while True:
            try:
                raw = await q.get()
            except asyncio.CancelledError:
                return
            info.last_activity = time.monotonic()
            if raw is None:
                # Session ended on the satellite — fan the sentinel out to the
                # active turn + every supervisor so none hang on a terminal that
                # will never come.
                if info.default_consumer is not None:
                    info.default_consumer.put_nowait(None)
                for cq in list(info.thread_consumers.values()):
                    cq.put_nowait(None)
                return
            # Synthetic turn-control markers (dicts without a codex ``method``).
            if isinstance(raw, dict) and "method" not in raw:
                if raw.get("type") == "_codex_thread_id":
                    # Capture the main thread id — the router needs it as the
                    # demux key, and it's sent once at session start (before the
                    # first turn registers a consumer), so the router consumes it
                    # directly instead of forwarding it to a (still-None) turn.
                    tid_new = raw.get("thread_id", "")
                    if tid_new and tid_new != info.codex_thread_id:
                        info.codex_thread_id = tid_new
                        try:
                            from storage.database import update_chat, get_chat_by_session
                            chat = get_chat_by_session(info.session_id)
                            if chat:
                                update_chat(chat["id"], codex_thread_id=tid_new)
                        except Exception:
                            logger.exception(
                                "Remote session %s: codex_thread_id persist failed",
                                info.session_id[:8],
                            )
                    continue
                # Other markers (_turn_ended / satellite error) → the active turn.
                if info.default_consumer is not None:
                    info.default_consumer.put_nowait(raw)
                continue
            params = raw.get("params") if isinstance(raw.get("params"), dict) else {}
            tid = params.get("threadId")
            if tid and info.codex_thread_id and tid != info.codex_thread_id:
                cq = info.thread_consumers.get(tid)
                if cq is None:
                    cq = asyncio.Queue()
                    info.thread_consumers[tid] = cq
                cq.put_nowait(raw)
            elif info.default_consumer is not None:
                info.default_consumer.put_nowait(raw)
            elif raw.get("method") in ("thread/goal/updated", "thread/goal/cleared"):
                # Codex accounts goal progress AT TURN STOP, so the final goal
                # update (often the completion) lands after turn/completed —
                # apply out-of-band (chat-durable state) instead of dropping.
                # Mirrors the LOCAL router's _apply_goal_oob.
                try:
                    from core.layers.codex.goals import apply_goal_events_oob
                    apply_goal_events_oob(
                        info.session_id, self._translate_codex_event(info, raw))
                except Exception:
                    logger.exception(
                        "Remote session %s: out-of-band goal apply failed",
                        info.session_id[:8],
                    )
            # else: between turns, a main-thread straggler with no active
            # consumer — drop it (the daemon is idle on the main thread).

    def _handoff_remote_bg_subagents(self, info: "RemoteSessionInfo") -> None:
        """At main-turn end: register + arm a supervisor for each background
        sub-agent still active (read from the translator, which saw the main
        thread's collabAgentToolCall states), drop buffers for foreground subs
        that already terminated, and clear the main-turn consumer. Feeds the
        per-session SubagentRegistry so the shared _bg_agent_monitor nudges and
        wait_for_bg_subagents covers remote delegated agents. Sync (mirrors the
        LOCAL _handoff_bg_subagents)."""
        info.default_consumer = None  # stop feeding the finished main turn
        pending: list[dict] = []
        if info.codex_translator is not None and info.alive:
            try:
                pending = info.codex_translator.pending_bg_subagents()
            except Exception:
                logger.exception(
                    "Remote session %s: pending_bg_subagents failed", info.session_id[:8],
                )
        pending_ids = {p["agent_id"] for p in pending}
        # Drop buffers for sub threads we won't supervise (foreground subs that
        # already reached terminal — their buffered events are dead weight).
        for tid in list(info.thread_consumers.keys()):
            if tid not in pending_ids and tid not in info.bg_supervisors:
                info.thread_consumers.pop(tid, None)
        if not pending:
            return
        from core.session.session_state import get_subagent_registry
        reg = get_subagent_registry(info.session_id)
        for p in pending:
            aid = p["agent_id"]
            if aid in info.bg_supervisors:
                continue  # already supervised (carried over from a prior turn)
            info.thread_consumers.setdefault(aid, asyncio.Queue())
            reg.register_spawn(aid, aid)
            info.bg_supervisors[aid] = asyncio.create_task(
                self._supervise_remote_bg_subagent(info, aid),
                name=f"remote-codex-bgsup-{info.session_id[:8]}-{aid[-6:]}",
            )
        logger.info(
            "Remote session %s: %d background sub-agent(s) running past turn end "
            "→ supervising %s", info.session_id[:8], len(pending), sorted(pending_ids),
        )

    async def _supervise_remote_bg_subagent(
        self, info: "RemoteSessionInfo", sub_tid: str,
    ) -> None:
        """Drain ONE background sub-agent's thread buffer until it terminates,
        then resolve it. Runs concurrently with user/nudge turns (never blocks a
        turn). 600 s ceiling backstops a lost terminal. Mirrors the LOCAL
        _supervise_bg_subagent."""
        CEILING = 600.0
        start = time.monotonic()
        q = info.thread_consumers.get(sub_tid)
        try:
            while q is not None and (time.monotonic() - start) < CEILING:
                try:
                    raw = await asyncio.wait_for(q.get(), timeout=5.0)
                except asyncio.TimeoutError:
                    if not info.alive:
                        break
                    continue
                info.last_activity = time.monotonic()
                if raw is None:  # session ended
                    break
                method = raw.get("method", "") if isinstance(raw, dict) else ""
                if method == "turn/completed":
                    break
                if method == "error":
                    err = (raw.get("params") or {}).get("error", {}) if isinstance(raw, dict) else {}
                    if not err.get("willRetry"):
                        break
            else:
                if q is not None:
                    logger.warning(
                        "Remote session %s: bg sub-agent %s hit the %.0fs ceiling "
                        "without a terminal — resolving",
                        info.session_id[:8], sub_tid, CEILING,
                    )
        finally:
            self._resolve_remote_bg_subagent(info, sub_tid)

    def _resolve_remote_bg_subagent(self, info: "RemoteSessionInfo", sub_tid: str) -> None:
        """Mark a remote background sub-agent done + clear its badge via the shared
        resolve_bg_subagent (one source of truth with the local path). Sync +
        idempotent; pops our own per-thread bookkeeping first."""
        from core.session.session_state import resolve_bg_subagent
        info.thread_consumers.pop(sub_tid, None)
        info.bg_supervisors.pop(sub_tid, None)
        resolve_bg_subagent(info.session_id, sub_tid, info.codex_translator)

    async def _teardown_remote_bg(self, info: "RemoteSessionInfo") -> None:
        """Cancel the router + every bg supervisor and resolve their registry
        entries (so close / offline can't leave the _bg_agent_monitor waiting on
        a dead sub-agent). Idempotent. Mirrors the LOCAL _teardown_bg."""
        if info.router_task is not None:
            info.router_task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await info.router_task
            info.router_task = None
        sups = list(info.bg_supervisors.items())
        for _sub_tid, task in sups:
            task.cancel()
        for sub_tid, task in sups:
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task
            # A cancelled supervisor's own finally already resolved it; this is
            # the idempotent backstop for one that never reached its finally.
            self._resolve_remote_bg_subagent(info, sub_tid)
        info.thread_consumers.clear()
        info.default_consumer = None
