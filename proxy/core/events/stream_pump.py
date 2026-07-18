"""ChatStreamPump — decoupled stream processor for dashboard chat turns.

Runs independently of WebSocket connections:
- Reads CommonEvent objects from producer via event queue
- Maintains live streaming state (_chat_streaming_state)
- Saves text and events to DB at turn boundaries
- Optionally forwards events to an attached WS subscriber

Execution-layer agnostic: consumes CommonEvent (not ClaudeStreamChunk).

Module-level state (_active_pumps, _pending_permissions,
_session_cumulative_cost) lives here and is imported by ws/dashboard.py.
"""

import asyncio
import json
import logging
import time
from datetime import datetime, timezone

from storage import database as task_store
from services.notifications import notification_manager
from core.events.artifact_events import artifact_event_from_perm_item
from core.session.session_state import (
    _chat_streaming_state,
    set_session_mode,
    get_subagent_registry,
)
from core.events.bg_command_state import get_bg_command_registry
from core.session.transcript_tool_events import result_summary, truncate_result
from core.events.common_events import (
    CommonEvent,
    TEXT, THINKING, TOOL_USE, TOOL_INPUT, TOOL_RESULT,
    PERMISSION_REQUEST, SUBAGENT_START, SUBAGENT_END, DELEGATE_SPAWN,
    BG_COMMAND_START, BG_COMMAND_END,
    WORKFLOW_START, WORKFLOW_PROGRESS, WORKFLOW_END,
    PLAN_MODE, SYSTEM, METADATA, DONE, ERROR, QUEUE_TURN, ARTIFACT_TURN,
    PRODUCER_DONE, TODO_UPDATE, GOAL_UPDATE, CONTEXT_COMPACT,
    goal_payload_to_state,
)

# Min seconds between workflow_progress WS forwards per workflow (the live
# tree is re-sent every tick; coalesce so a busy workflow can't flood the
# satellite WS send-queue / frame cap). The terminal
# frame and WORKFLOW_END always go through.
_WORKFLOW_PROGRESS_MIN_INTERVAL = 0.3

# LLM chat-title generation: fire the one-time title upgrade once the first
# assistant response crosses this many characters on the first turn (~70 tokens
# — enough signal for a good title, early enough to feel instant). A shorter
# first response titles at PRODUCER_DONE instead. See services/title_generator.py.
_TITLE_CHAR_THRESHOLD = 280

logger = logging.getLogger("claude-proxy")


# ---------------------------------------------------------------------------
# Module-level state for dashboard streaming
# ---------------------------------------------------------------------------

# Pending permission prompts per session (survives WebSocket reconnects)
_pending_permissions: dict[str, dict] = {}  # session_id -> perm event data

# Active stream pumps per chat — decoupled from WebSocket connections.
# When a WS detaches (chat switch, browser close), the pump keeps running.
# When a WS reconnects, it attaches to the existing pump for live updates.
_active_pumps: dict[str, "ChatStreamPump"] = {}  # chat_id -> pump

# Chats whose in-flight turn will be RE-ADOPTED from the satellite after a
# proxy restart (Mode C). During graceful shutdown the pump must NOT persist
# the partial turn's blocks or cost — the recovery replay re-persists the
# full turn, so a shutdown flush would duplicate content + double-count cost.
_recovery_suppress_flush: set[str] = set()  # chat_id


def suppress_recovery_flush(chat_id: str) -> None:
    """Mark a chat's in-flight turn for satellite re-adopt: its pump skips the
    durable turn-block + cost persist so the post-restart replay doesn't
    duplicate them."""
    _recovery_suppress_flush.add(chat_id)

# Tracks CLI's cumulative cost per session across pump instances.
# CLI's total_cost_usd is cumulative per process — new pumps need the last
# reported value to compute per-turn deltas correctly.
_session_cumulative_cost: dict[str, float] = {}  # session_id -> last CLI cost

# Initial _current_goal marker, distinct from None ("goal known cleared") so
# the change-gate can't skip NULLing chats.thread_goal on a cleared event in a
# pump that never saw the goal being set.
_GOAL_UNSET = object()


class ChatStreamPump:
    """Decoupled stream processor for chat turns.

    Runs independently of WebSocket connections:
    - Reads CommonEvent objects from producer via event_queue
    - Maintains live streaming state (_chat_streaming_state)
    - Saves text and events to DB at turn boundaries
    - Optionally forwards events to an attached WS subscriber

    When no WS is attached, the pump continues saving to DB.
    When a WS attaches (or re-attaches), it gets the live stream.
    """

    def __init__(
        self,
        chat_id: str,
        session_id: str,
        producer: asyncio.Task,
        event_queue: asyncio.Queue,
        perm_queue: asyncio.Queue | None,
        implementing_plan: str = "",
        scope: str = "user",
        source_type: str = "chat",
    ):
        self.chat_id = chat_id
        self.session_id = session_id
        self.producer = producer
        self.event_queue = event_queue
        self.perm_queue = perm_queue
        self.implementing_plan = implementing_plan
        self.scope = scope
        self.source_type = source_type

        self._ws_queue: asyncio.Queue | None = None
        self._done = False
        self._abort_requested = False  # abort() sets; drives the resumed-task run-status close
        self._task: asyncio.Task | None = None

        # Message queue (shared with producer closure)
        self.message_queue: list[str] = []
        # System prompt queue — delivered silently (no user bubble).
        # Used for delegate results and bg nudges during background drain.
        self.system_queue: list[str] = []
        # Artifact-interaction queue (shared with producer closure) — pending
        # otodock.send payloads from display_ui artifacts, delivered as their
        # own framed turn(s) at the boundary (ws/artifact_interactions.py).
        self.artifact_queue: list[dict] = []

        # Subagent spawn/finish tracking is authoritative in the per-session
        # SubagentRegistry (core/session/session_state.py), keyed by session_id —
        # the SubagentStop hook reaches it out-of-band, and it's id-keyed so
        # parallel agents finishing out of order are tracked correctly.
        # Live dynamic-workflow trees (keyed by tool_use_id) — last WS-forward
        # time per workflow for progress coalescing.
        self._wf_last_forward: dict[str, float] = {}
        # Last chats.updated_at bump — sidebar recency. Interactive turns get
        # this for free (the transcript tailer persists rows continuously); a
        # headless turn can sit inside one long tool call persisting nothing,
        # sinking a GENERATING chat down the sidebar. Any stream event is
        # activity; the touch is throttled in the event loop.
        self._activity_touch = time.monotonic()
        self._pending_text: list[str] = []  # text chunks since last event
        self._turn_blocks: list[dict] = []  # ordered text segments + events
        # Highest DB message id before this pump started — the id-based cutoff that
        # excludes in-flight turn saves from chat_history on live reconnect. id-based
        # (not a row count) so a *windowed* resume can still withhold the in-flight
        # tail: a count can't index a 50-row page (see ws/dashboard.py truncation).
        self._db_msg_cutoff_id = task_store.get_last_chat_message_id(chat_id)
        # LLM chat-title generation (services/title_generator.py): arm the
        # one-time title upgrade for chats not yet LLM-titled. Fired in the
        # TEXT handler once the first response crosses _TITLE_CHAR_THRESHOLD,
        # or at PRODUCER_DONE for a short first response. Task chats title
        # like every chat (they live in the sidebar's Task history view);
        # only meetings are excluded. The atomic claim guarantees
        # exactly-once across both fire points and any later turn.
        _title_chat = task_store.get_chat(chat_id) or {}
        self._title_armed = (
            not chat_id.startswith("meeting-")
            and not _title_chat.get("title_generated")
        )
        self._active_tools: dict[str, dict] = {}
        self._pending_previews: dict[str, dict] = {}  # file_id -> latest preview (flushed at turn end)
        # Armed by a preview flush; _save_turn_blocks then prunes this chat's
        # unreferenced version-pinned snapshots (reference-driven GC — must run
        # AFTER the flushed rows persist, or their snapshots look orphaned).
        self._gc_snapshots_pending = False
        self._thinking_text = ""
        self._total_cost_delta = 0.0
        # LLM-only delta (METADATA events) — used to write the LLM usage_records
        # row distinct from per-MCP rows. Computing the LLM share as
        # `_total_cost_delta - sum(_mcp_cost_by_key.values())` would drift due
        # to float subtraction; tracking it directly avoids that.
        self._llm_cost_delta = 0.0
        # Per-(provider, model) MCP cost accumulated this turn — one
        # usage_records row per key at PRODUCER_DONE. See mcp_cost_engine.
        self._mcp_cost_by_key: dict[tuple[str, str], float] = {}
        # Seed from previous pump's last CLI cost (cumulative per process)
        self._last_session_cost = _session_cumulative_cost.get(session_id, 0.0)
        self._cost_saved = False
        self._meeting_agent: str | None = None  # current speaker in meetings
        self._context_used = 0
        self._context_max = 0
        self._cache_read = 0
        self._cache_write = 0
        self._input_tokens = 0
        self._output_tokens = 0

        # Plan filename: reuse across reviews in the same chat to avoid duplicates
        self._plan_filename: str = ""

        # Current todo list from TodoWrite (for live_state reconnection)
        self._current_todos: list[dict] = []
        # Codex thread goal (GOAL_UPDATE) — mirrored in live.goal + written
        # through to chats.thread_goal on change. Starts UNSET (not None): a
        # cleared event in a later pump must still NULL the column even though
        # this pump never saw the goal being set.
        self._current_goal = _GOAL_UNSET
        # Codex update_plan checklist persisted as a TodoWrite-shaped block
        # (one per turn-segment, updated in place; reset in _save_turn_blocks).
        self._todo_block: dict | None = None

        # Permission gating: only one blocking prompt shown at a time
        self._permission_active: dict | None = None  # currently shown permission
        self._permission_buffer: list[dict] = []  # queued permissions waiting

    # Tools that have dedicated events (task_spawn, plan_mode) — skip tool persistence
    _SKIP_TOOL_PERSIST = frozenset({"Agent", "Task", "EnterPlanMode", "ExitPlanMode", "mcp__delegation-mcp__delegate"})

    @property
    def is_done(self) -> bool:
        return self._done

    def attach(self) -> asyncio.Queue:
        """Attach a WS consumer. Returns queue to read pump events from."""
        q: asyncio.Queue = asyncio.Queue()
        old = self._ws_queue
        self._ws_queue = q
        if old:
            old.put_nowait({"pump_type": "detached"})
        return q

    def detach(self, q: asyncio.Queue):
        """Detach WS consumer. Pump continues independently.

        ``q`` is the queue the caller received from :meth:`attach` — a holder
        may only detach ITSELF. If another connection attached since (attach()
        swapped the queue), this is a no-op: a stale detach (an old socket's
        close/disconnect path racing a new viewer's attach) must not stop the
        new viewer's frames mid-stream.
        """
        if self._ws_queue is not q:
            return
        self._ws_queue = None

    def abort(self):
        """Kill the producer — pump will exit its loop."""
        self._abort_requested = True
        self.producer.cancel()
        # Flip live-state agents/delegates/workflows inactive so a late
        # live_state snapshot (e.g. a reconnect after the abort) can't resurrect
        # cleared work — the frontend badges derive from these blocks' active
        # flags. The dicts are the same refs held in live_blocks, so this also
        # updates the ordered reconstruction list.
        live = _chat_streaming_state.get(self.chat_id)
        if live:
            for a in live.get("active_agents", []):
                a["active"] = False
            for d in live.get("active_delegates", []):
                d["active"] = False
            for c in live.get("active_commands", []):
                c["active"] = False
            for w in live.get("workflows", {}).values():
                w["active"] = False

    async def resolve_active_permission(self):
        """Current permission resolved. Forward next from buffer if any."""
        self._permission_active = None
        live = _chat_streaming_state.get(self.chat_id)
        if live:
            live["pending_permission"] = None

        if self._permission_buffer:
            # Show next queued permission
            next_perm = self._permission_buffer.pop(0)
            await self._show_permission(next_perm)
        else:
            # No more pending — clear reconnect storage
            _pending_permissions.pop(self.session_id, None)

    async def _show_permission(self, perm_data: dict):
        """Set a permission as active and forward to WS."""
        self._permission_active = perm_data
        _pending_permissions[self.session_id] = perm_data
        live = _chat_streaming_state.get(self.chat_id)
        if live:
            live["pending_permission"] = perm_data

        evt_type = perm_data.get("event_type", "")
        if evt_type == "plan_review":
            filename = perm_data.get("filename", "")
            await self._forward(
                {"pump_type": "perm_plan_review", "perm_data": perm_data,
                 "filename": filename},
            )
        elif evt_type == "question_prompt":
            # Codex request_user_input: the daemon HOLDS the turn open on this
            # question, so the turn-end ping never fires for it. Surface the card
            # and fire a "needs your input" ephemeral NOW (away/FCM aware) so a
            # user who stepped away still gets pinged. Do NOT touch chat_status —
            # the turn is still streaming (no "ready" flip mid-held-turn).
            await self._forward(
                {"pump_type": "perm_question_prompt", "perm_data": perm_data},
            )
            chat = task_store.get_chat(self.chat_id)
            if (chat and self.source_type != "task"
                    and not task_store.get_active_meeting_for_chat(self.chat_id)):
                qs = (perm_data.get("tool_input") or {}).get("questions") or []
                first_q = (qs[0].get("question") if qs and isinstance(qs[0], dict)
                           else "") or "Waiting for your answer"
                asyncio.create_task(notification_manager.fire_ephemeral(
                    chat["user_sub"],
                    title=f"{chat['agent']} needs your input",
                    body=first_q,
                    chat_id=self.chat_id,
                ))
        else:
            forward_data = {"pump_type": "perm_permission_prompt", "perm_data": perm_data}
            # Include meeting agent identity so the frontend can show which agent is asking
            if perm_data.get("meeting_agent"):
                forward_data["meeting_agent"] = perm_data["meeting_agent"]
            await self._forward(forward_data)

    async def _queue_or_show_permission(self, perm_data: dict):
        """Show permission if none active, otherwise buffer it."""
        if self._permission_active is None:
            await self._show_permission(perm_data)
        else:
            self._permission_buffer.append(perm_data)

    def queue_message(self, text: str) -> int:
        """Queue a user message for the producer. Returns index."""
        self.message_queue.append(text)
        return len(self.message_queue) - 1

    def cancel_queued(self, index: int) -> str | None:
        """Remove queued message by index. Returns removed text or None."""
        if 0 <= index < len(self.message_queue):
            return self.message_queue.pop(index)
        return None

    def cancel_all_queued(self) -> str:
        """Remove all queued messages (artifact interactions too — they were
        never delivered, so nothing persists). Returns combined user text."""
        combined = "\n\n".join(self.message_queue) if self.message_queue else ""
        self.message_queue.clear()
        self.artifact_queue.clear()
        return combined

    def queue_artifact(self, interaction: dict) -> bool:
        """Queue an artifact interaction for the boundary drain. False when
        the pending cap is hit (each delivery costs a real agent turn)."""
        from ws.artifact_interactions import QUEUE_CAP
        if len(self.artifact_queue) >= QUEUE_CAP:
            return False
        self.artifact_queue.append(interaction)
        return True

    def _flush_pending_text(self):
        """Flush accumulated text to _turn_blocks as a text segment.

        Captures the current _meeting_agent so text blocks retain agent
        identity even when saved later (e.g. at PRODUCER_DONE when
        _meeting_agent has already been cleared by meeting_concluded).
        """
        text = "".join(self._pending_text)
        if text:
            self._turn_blocks.append(self._stamp_speaker({"type": "text", "content": text}))
            self._pending_text.clear()

    def _stamp_speaker(self, block: dict) -> dict:
        """Stamp the current meeting speaker onto a block before it persists.

        Layer events spread the orchestrator's tag via ``**ed``
        (task_spawn/bg_command_spawn/delegate_spawn/system); this covers the
        PUMP-built blocks — text, thinking, tool, checklist, and the
        hook-queue artifact/question blocks — so their cards carry the
        speaker identity too. No-op outside meetings.
        """
        if self._meeting_agent:
            block["_meeting_agent"] = self._meeting_agent
        return block

    async def _flush_pending_previews(self):
        """Forward buffered document previews to dashboard (called at turn end)."""
        if self._pending_previews:
            self._gc_snapshots_pending = True
        for evt in self._pending_previews.values():
            await self._forward({"pump_type": "ws_event", "event": evt})
        self._pending_previews.clear()

    def _save_turn_blocks(self):
        """Save all turn blocks to DB in order (preserves interleaving).

        After saving, advances _db_msg_cutoff_id so that these messages are
        included in chat_history on WS reconnect (not truncated as
        in-progress streaming content).
        """
        if self.chat_id in _recovery_suppress_flush:
            # This turn will be re-adopted + re-persisted from the satellite
            # after the restart — a shutdown flush here would duplicate it.
            self._turn_blocks.clear()
            return
        if not self._turn_blocks:
            return
        for block in self._turn_blocks:
            if block["type"] == "media_processing":
                # Transient transcode skeleton — never persisted. If a turn ends
                # while a transcode is still running, the placeholder is dropped
                # rather than frozen into history.
                continue
            if block["type"] == "text":
                meeting_agent = block.get("_meeting_agent")
                event_data = ""
                if meeting_agent:
                    from storage import agent_store
                    ad = agent_store.get_agent(meeting_agent)
                    event_data = json.dumps({
                        "agent_slug": meeting_agent,
                        "agent_display_name": (ad or {}).get("display_name", meeting_agent),
                        "agent_color": (ad or {}).get("color", ""),
                        "badge": "meeting",
                    })
                task_store.add_chat_message(self.chat_id, "assistant", block["content"],
                                            event_data=event_data)
            else:
                task_store.add_chat_message(
                    self.chat_id, "event", "",
                    event_type=block["type"],
                    event_data=json.dumps(block),
                )
        self._turn_blocks.clear()
        self._todo_block = None
        if self._gc_snapshots_pending:
            # The flushed preview rows are persisted now — prune this chat's
            # unreferenced version-pinned snapshots (dismissed history etc.).
            self._gc_snapshots_pending = False
            try:
                from services.media import preview_snapshots
                preview_snapshots.gc_chat(self.chat_id)
            except Exception:
                logger.debug("preview snapshot GC failed", exc_info=True)
        # Advance cutoff so incrementally saved content (completed meeting
        # turns) is included in chat_history on reconnect, not truncated.
        self._db_msg_cutoff_id = task_store.get_last_chat_message_id(self.chat_id)
        # Clear live_blocks for the saved content — it's now in DB and will
        # be sent via chat_history. Only unsaved content (current in-progress
        # turn) should remain in live_blocks for live_state reconnection.
        live = _chat_streaming_state.get(self.chat_id)
        if live:
            live["live_blocks"] = []

    def _record_usage(self, chat_row: dict):
        """Record one usage_records row for the LLM + one per (provider, model)
        for any per-tool MCP costs accumulated this turn.

        The LLM row is always written (even at $0 cost) so token counts and
        the message_count survive — analytics depends on them. MCP rows are
        only written when their cost is positive.
        """
        try:
            from services.billing import usage_service
            from services.engines import subscription_pool
            from config import get_model_provider

            chat_model = chat_row.get("model", "")
            llm_provider = get_model_provider(chat_model) or "anthropic"
            llm_cost = max(0.0, round(self._llm_cost_delta, 6))

            common = {
                "user_sub": chat_row.get("user_sub"),
                "agent": chat_row.get("agent", ""),
                "scope": self.scope,
                "source_type": self.source_type,
                "source_id": self.chat_id,
            }

            # Attribute the LLM turn to the subscription that served it, so the pool
            # can route new chats to the least-consumed account (headroom routing).
            sub_id = subscription_pool.get_session_subscription(self.session_id) or "default"

            rows: list[dict] = [{
                **common,
                "cost_usd": llm_cost,
                "input_tokens": self._input_tokens,
                "output_tokens": self._output_tokens,
                "cache_read": self._cache_read,
                "cache_write": self._cache_write,
                "message_count": 1,
                "provider": llm_provider,
                "model": chat_model,
                "source_key": sub_id,
            }]
            for (provider, model), cost in self._mcp_cost_by_key.items():
                if cost <= 0:
                    continue
                rows.append({
                    **common,
                    "cost_usd": round(cost, 6),
                    "input_tokens": 0,
                    "output_tokens": 0,
                    "cache_read": 0,
                    "cache_write": 0,
                    "message_count": 0,
                    "provider": provider,
                    "model": model,
                })

            usage_service.record_turn_usage(rows)
        except Exception as e:
            logger.error(f"Failed to record usage: {e}")

    async def _forward(self, item: dict):
        """Forward event to WS subscriber if attached."""
        if self._ws_queue:
            await self._ws_queue.put(item)

    def start(self) -> asyncio.Task:
        """Start the pump's background processing task."""
        self._task = asyncio.create_task(self._run())
        return self._task

    async def _run(self):
        """Main pump loop — reads CommonEvent objects, updates state, saves to DB."""
        _chat_streaming_state[self.chat_id] = {
            "streaming": True,
            "session_id": self.session_id,
            "started_at": time.time(),
            "live_blocks": [],       # ordered list — preserves text/widget interleaving
            "active_tools": [],      # still-running tools (for status bar)
            "active_agents": [],     # all agents with active flag (for status bar)
            "active_delegates": [],  # all delegates (for status bar)
            "active_commands": [],   # background bash commands with active flag (for status bar)
            "pending_permission": None,
            "thinking_active": False,
            "thinking_text": "",
            "thinking_tokens": 0,    # live-only ~token gauge (adaptive-effort models)
            "todos": [],             # current TodoWrite checklist for floating panel
            "goal": None,            # codex thread goal for the GoalPanel
            "meeting_agent": None,   # current speaker in meetings
            "meeting_participants": [],  # participant list for MeetingIndicator
            "workflows": {},         # tool_use_id → live dynamic-workflow tree
        }
        # Let the SubagentStop hook endpoint route per-agent completions back
        # to this chat without a DB lookup (DB lookup is the fallback).
        get_subagent_registry(self.session_id).chat_id = self.chat_id
        get_bg_command_registry(self.session_id).chat_id = self.chat_id

        # Light this chat's sidebar dot on every device of its owner — the
        # authoritative turn-start signal, viewed or background. The matching
        # "ready" fires in the finally below when the turn genuinely ends.
        # Shared-only chats (synthetic agent:: owner) fan out to every user of
        # the agent via the agent arg.
        owner_row = task_store.get_chat(self.chat_id) or {}
        self._owner_sub = owner_row.get("user_sub") or ""
        if self._owner_sub:
            notification_manager.broadcast_chat_status(
                self._owner_sub, self.chat_id, "streaming",
                agent=owner_row.get("agent") or "",
            )
        if self.chat_id.startswith("task-run-") and self.source_type != "task":
            # Dashboard-RESUMED task conversation (the scheduler's own runs are
            # source_type == "task"): reflect the live turn in the Task History
            # row, which otherwise freezes on its pre-resume terminal state.
            # Terminal-states-only guard: never touch a run some other pump or
            # the scheduler still owns. The matching close runs in the finally.
            try:
                task_store.update_latest_run_status_for_chat(
                    self.chat_id, "running",
                    only_from=("completed", "failed", "cancelled", "limit_exceeded"),
                )
            except Exception:
                pass

        try:
            while True:
                # Poll permission queue (non-blocking)
                if self.perm_queue is not None:
                    try:
                        perm_data = self.perm_queue.get_nowait()
                        await self._handle_perm_event(perm_data)
                    except asyncio.QueueEmpty:
                        pass

                # Read CommonEvent from event queue (faster poll when WS attached)
                timeout = 0.15 if self._ws_queue else 2.0
                try:
                    event: CommonEvent = await asyncio.wait_for(
                        self.event_queue.get(), timeout=timeout,
                    )
                except asyncio.TimeoutError:
                    continue

                # Throttled sidebar-recency touch (see _activity_touch above).
                _touch_now = time.monotonic()
                if _touch_now - self._activity_touch >= 60.0:
                    self._activity_touch = _touch_now
                    try:
                        await asyncio.to_thread(task_store.touch_chat, self.chat_id)
                    except Exception:
                        pass

                if event.type == ERROR:
                    err_msg = event.data.get("message", "")
                    # Failover: if the turn died on a provider limit/overload, rest
                    # this session's subscription so the next chat/turn picks another.
                    # A real rate/usage limit gets the full cooldown; a transient
                    # overload (529) gets only a brief nudge (the account is fine and
                    # the CLI retries) so an immediate retry isn't blocked.
                    try:
                        from services.engines import subscription_pool as _subpool
                        _cooldown = _subpool.throttle_cooldown_for(err_msg)
                        if _cooldown:
                            _subpool.mark_subscription_throttled(
                                self.session_id, cooldown_s=_cooldown,
                            )
                    except Exception:
                        pass
                    await self._forward({"pump_type": "error", "message": err_msg})
                    # For an interruption that genuinely lost
                    # output (satellite reconnect-grace expiry — `durable_marker`),
                    # persist a visible ⚠ block so a refresh shows it instead of a
                    # silent truncation. This loop BREAKS on ERROR (the trailing
                    # DONE never flows through the pump), but the `finally` runs
                    # _flush_pending_text() + _save_turn_blocks() on exit — so
                    # flush the partial turn text FIRST, then append the marker,
                    # and both land in chat_history in order. Ordinary transient
                    # errors omit the flag → today's live-only behaviour.
                    if event.data.get("durable_marker") and err_msg:
                        self._flush_pending_text()
                        self._turn_blocks.append({"type": "text", "content": err_msg})
                    break

                if event.type == PRODUCER_DONE:
                    # Final perm queue drain
                    if self.perm_queue is not None:
                        while True:
                            try:
                                perm_data = self.perm_queue.get_nowait()
                                await self._handle_perm_event(perm_data)
                            except asyncio.QueueEmpty:
                                break
                    # Save remaining text/events in order
                    self._flush_pending_text()
                    await self._flush_pending_previews()
                    for tool_evt in self._active_tools.values():
                        self._turn_blocks.append(tool_evt)
                    self._active_tools.clear()
                    self._save_turn_blocks()
                    # Persist cost + context. Fire on ANY token activity, not
                    # only cost>0, so $0 turns (local / unpriced providers) still
                    # persist token counts + message_count (analytics needs them).
                    has_activity = (self._total_cost_delta > 0
                                    or self._input_tokens or self._output_tokens)
                    if self.chat_id in _recovery_suppress_flush:
                        has_activity = False  # replay re-persists; don't double
                    if has_activity and not self._cost_saved:
                        self._cost_saved = True
                        chat_row = task_store.get_chat(self.chat_id)
                        if chat_row:
                            old_cost = chat_row.get("total_cost") or 0
                            updates = {"total_cost": old_cost + self._total_cost_delta}
                            if self._context_used > 0:
                                updates["context_used"] = self._context_used
                                updates["context_max"] = self._context_max
                            if self._cache_write > 0:
                                updates["cache_read"] = self._cache_read
                                updates["cache_write"] = self._cache_write
                                updates["output_tokens"] = self._output_tokens
                            task_store.update_chat(self.chat_id, **updates)
                            self._record_usage(chat_row)
                    # LLM chat-title: a short first response never crossed the
                    # streaming threshold — fire once at turn-end now that the
                    # assistant message is persisted (the service reads the first
                    # prompt + response from the DB). The atomic claim dedupes
                    # against the threshold fire. One pump == one turn.
                    if self._title_armed:
                        self._title_armed = False
                        from services import title_generator
                        asyncio.create_task(
                            title_generator.request_chat_title(self.chat_id)
                        )
                    await self._forward({"pump_type": "all_done"})
                    break

                if event.type == QUEUE_TURN:
                    # During a meeting turn, flush+save accumulated blocks first
                    # so the user message appears after prior content in DB order.
                    if self._meeting_agent:
                        self._flush_pending_text()
                        if self._turn_blocks:
                            self._save_turn_blocks()
                    meeting_agent = event.data.get("meeting_agent")
                    event_data_str = ""
                    if meeting_agent:
                        from storage import agent_store as _agent_store
                        ad = _agent_store.get_agent(meeting_agent)
                        event_data_str = json.dumps({
                            "agent_slug": meeting_agent,
                            "agent_display_name": (ad or {}).get("display_name", meeting_agent),
                            "agent_color": (ad or {}).get("color", ""),
                            "badge": "meeting prompt",
                        })
                    task_store.add_chat_message(self.chat_id, "user", event.data["text"],
                                                event_data=event_data_str)
                    await self._forward({"pump_type": "queue_turn", "text": event.data["text"]})
                    continue

                if event.type == ARTIFACT_TURN:
                    # Drained backchannel interactions (artifact sends AND
                    # mini-app send_prompt actions, kind-tagged): one distinct
                    # event row per entry (NEVER a "user" row — the row type +
                    # the framed prompt carry the provenance), forwarded live
                    # so the sender's transcript shows the chip at delivery.
                    from ws import artifact_interactions as _ai
                    for it in event.data.get("interactions", []):
                        task_store.add_chat_message(
                            self.chat_id, "event", "",
                            event_type=_ai.event_type(it),
                            event_data=_ai.event_row_json(it),
                        )
                        frame = _ai.ws_frame(it, self.chat_id)
                        frame["pump_type"] = frame.pop("type")
                        await self._forward(frame)
                    continue

                # Process event (text, thinking, tools, metadata, etc.)
                await self._process_event(event)

        except Exception as e:
            logger.error(
                f"ChatStreamPump error: chat={self.chat_id}, error={e}",
                exc_info=True,
            )
        finally:
            self._done = True
            self.producer.cancel()
            try:
                await self.producer
            except (asyncio.CancelledError, Exception):
                pass

            # Save any unflushed text/events (error recovery)
            self._flush_pending_text()
            await self._flush_pending_previews()
            # Persist in-progress thinking text too — if the producer died
            # mid-stream (e.g. asyncio.LimitOverrunError on a large MCP
            # tool-result line), thinking events between phase=start and
            # phase=end would otherwise never reach _turn_blocks and the
            # block would silently vanish on chat refresh.
            if self._thinking_text:
                self._turn_blocks.append(self._stamp_speaker({
                    "type": "thinking", "content": self._thinking_text,
                }))
                self._thinking_text = ""
            for tool_evt in self._active_tools.values():
                self._turn_blocks.append(tool_evt)
            self._save_turn_blocks()

            has_activity = (self._total_cost_delta > 0
                            or self._input_tokens or self._output_tokens)
            if self.chat_id in _recovery_suppress_flush:
                has_activity = False  # replay re-persists cost; don't double
            if has_activity and not self._cost_saved:
                self._cost_saved = True
                chat_row = task_store.get_chat(self.chat_id)
                if chat_row:
                    old_cost = chat_row.get("total_cost") or 0
                    updates = {"total_cost": old_cost + self._total_cost_delta}
                    if self._context_used > 0:
                        updates["context_used"] = self._context_used
                        updates["context_max"] = self._context_max
                    if self._cache_write > 0:
                        updates["cache_read"] = self._cache_read
                        updates["cache_write"] = self._cache_write
                        updates["output_tokens"] = self._output_tokens
                    task_store.update_chat(self.chat_id, **updates)
                    self._record_usage(chat_row)

            if self.implementing_plan:
                task_store.update_chat_plan_status(
                    self.chat_id, self.implementing_plan, "implemented",
                )

            # Clear live state + deregister (only if we're still the current pump).
            # EXCEPTION: while background subagents are still running, keep a
            # reduced live-state residual — the running-agent badges only — so a
            # reconnect between this turn's end and the nudge turn still renders
            # them. The turn's text/widgets are already persisted to the DB
            # (saved above at PRODUCER_DONE), so we DROP live_blocks to avoid
            # double-rendering against chat history; the status bar's active_agents
            # is the only thing the DB can't reconstruct mid-flight. Each agent's
            # badge clears via mark_subagent_done as it finishes; the nudge turn's
            # pump pops the state fully when has_pending is finally False.
            # Supersession guard: a NEWER pump may already own this chat — an
            # abort + fast resend starts the next turn's pump while this one is
            # still unwinding. Only the still-current pump may tear down shared
            # per-chat state or declare the chat ready below; a late pop /
            # "ready" broadcast from the aborted pump would wipe the new turn's
            # live state and flip the live UI idle mid-turn (the session_id
            # check alone doesn't cover Codex, where abort keeps the daemon and
            # the next turn reuses the same session_id).
            was_active_pump = _active_pumps.get(self.chat_id) is self
            live = _chat_streaming_state.get(self.chat_id)
            if was_active_pump and live and live.get("session_id") == self.session_id:
                if (get_subagent_registry(self.session_id).has_pending
                        or get_bg_command_registry(self.session_id).has_pending):
                    live["streaming"] = False
                    live["live_blocks"] = []
                    live["active_tools"] = []
                    live["pending_permission"] = None
                    live["thinking_active"] = False
                    live["thinking_text"] = ""
                    live["thinking_tokens"] = 0
                    live["todos"] = []
                    live["active_agents"] = [
                        a for a in live.get("active_agents", []) if a.get("active")
                    ]
                    live["active_delegates"] = [
                        d for d in live.get("active_delegates", []) if d.get("active")
                    ]
                    live["active_commands"] = [
                        c for c in live.get("active_commands", []) if c.get("active")
                    ]
                else:
                    _chat_streaming_state.pop(self.chat_id, None)
            if was_active_pump:
                del _active_pumps[self.chat_id]
                # A response (possibly partial, on abort) landed on this chat —
                # stamp it for the sidebar unread indicator. Superseded pumps
                # skip (the newer pump owns the chat's lifecycle).
                try:
                    task_store.update_chat(
                        self.chat_id,
                        last_response_at=datetime.now(timezone.utc).isoformat(),
                    )
                except Exception:
                    pass
                if self.chat_id.startswith("task-run-") and self.source_type != "task":
                    # Close the resumed-task History row (mirror of the
                    # 'running' flip at turn start): the user stopping the
                    # turn reads as cancelled, everything else as completed.
                    # only_from=("running",): close ONLY a turn this pump
                    # opened — a wedged-pump reap stamps failed + reason
                    # before aborting us, and that verdict must survive.
                    try:
                        task_store.update_latest_run_status_for_chat(
                            self.chat_id,
                            "cancelled" if self._abort_requested else "completed",
                            only_from=("running",),
                        )
                    except Exception:
                        pass

            # Signal any remaining subscriber
            if self._ws_queue:
                self._ws_queue.put_nowait({"pump_type": "pump_ended"})

            # Fire the ephemeral turn-complete signal — but NOT while background
            # subagents are still running. When the LLM spawns bg agents and ends
            # its turn ("launched, I'll review when done"), the work is not
            # actually finished; the genuine completion is the nudge turn that
            # fires once every SubagentStop has landed. Notifying here would alert
            # "finished" prematurely (and, for the in-app ping, twice). The nudge
            # turn's pump end has no pending agents, so it fires the real one.
            # notification_manager decides foreground (in-app onDone ping) vs
            # backgrounded (FCM vibration) — we just gate on "genuinely done".
            try:
                if not was_active_pump:
                    # Superseded by a newer pump for this chat: it broadcast
                    # "streaming" at its _run entry, so OUR "ready" would land
                    # after it and clear the live dot/stop button mid-turn.
                    # The new pump owns the ready broadcast + notification.
                    logger.debug(
                        f"chat={self.chat_id}: superseded pump — skipping ready "
                        f"broadcast + end-of-turn notification"
                    )
                elif get_subagent_registry(self.session_id).has_pending:
                    logger.debug(
                        f"chat={self.chat_id}: end-of-turn notification deferred — "
                        f"background subagents still running (nudge turn will fire it)"
                    )
                else:
                    self._fire_end_of_turn()
            except Exception:
                pass  # Don't break pump cleanup for notification failure

            logger.info(
                f"ChatStreamPump ended: chat={self.chat_id}, "
                f"blocks={len(self._turn_blocks)}, plan={self.implementing_plan or 'none'}"
            )

    def _fire_end_of_turn(self) -> None:
        """The turn genuinely ended: clear the sidebar dot on every device (a
        background chat has no other live signal), then fire the origin-routed
        end-of-turn alert. The alert is skipped during meetings (per-speaker
        turn ends are not completions) and for scheduled task runs
        (``source_type == "task"``) — a task's completion alert is its
        ``notification_mode`` contract, and an extra "finished" push on top is
        noise. A continued (re-warmed) task chat runs through the dashboard
        pump (``source_type == "chat"``) and keeps the normal per-turn signal —
        it is the only completion signal those follow-up turns have."""
        chat = task_store.get_chat(self.chat_id)
        if not chat:
            return
        notification_manager.broadcast_chat_status(
            chat["user_sub"], self.chat_id, "ready",
            agent=chat.get("agent") or "",
        )
        if self.source_type == "task":
            return
        if not task_store.get_active_meeting_for_chat(self.chat_id):
            asyncio.create_task(notification_manager.fire_ephemeral(
                chat["user_sub"],
                title=f"{chat['agent']} finished",
                body="Response ready",
                chat_id=self.chat_id,
            ))

    @staticmethod
    def _live_append_text(live: dict, text: str):
        """Append text to the last text block in live_blocks, or create a new one."""
        blocks = live["live_blocks"]
        if blocks and blocks[-1].get("type") == "text":
            blocks[-1]["content"] += text
        else:
            blocks.append({"type": "text", "content": text})

    async def _clear_orphan_thinking(self, live: dict | None):
        """Real output started while a progress-only thinking was still live.

        Defensive for CLI shapes that send `thinking_tokens` pings without the
        empty thinking block's phase=end (the confirmed Opus 4.7+/4.8 shape
        DOES send start+end, so this is normally a no-op — real flows clear at
        phase=end first, and `_thinking_text` is only non-empty when content
        actually streamed, which always ends with its own phase=end)."""
        if live and live.get("thinking_active") and not self._thinking_text:
            live["thinking_active"] = False
            live["thinking_tokens"] = 0
            await self._forward(
                {"pump_type": "ws_event",
                 "event": {"type": "thinking", "phase": "end", "text": ""}},
            )

    async def _process_event(self, event: CommonEvent):
        """Process a single CommonEvent — update live state, save events, forward.

        Text is accumulated in _pending_text. Before any non-text event,
        pending text is flushed to _turn_blocks. This preserves interleaving
        so widgets appear in correct position after page refresh.
        live_blocks mirrors this interleaving for reconnect rendering.

        Execution-layer agnostic: operates on CommonEvent, not ClaudeStreamChunk.
        """
        live = _chat_streaming_state.get(self.chat_id)
        ed = event.data

        if event.type == TEXT:
            content = ed.get("content", "")
            if content:
                await self._clear_orphan_thinking(live)
                await self._forward(
                    {"pump_type": "ws_event", "event": {"type": "text", "content": content}},
                )
                self._pending_text.append(content)
                if live:
                    self._live_append_text(live, content)
                # LLM chat-title: once the first response crosses the length
                # threshold on the first turn, fire the title upgrade early (feels
                # instant) from the response so far. Disarmed immediately; the
                # PRODUCER_DONE fallback covers shorter turns; the atomic claim
                # guarantees exactly-once.
                if self._title_armed and sum(
                    len(t) for t in self._pending_text
                ) >= _TITLE_CHAR_THRESHOLD:
                    self._title_armed = False
                    from services import title_generator
                    asyncio.create_task(title_generator.request_chat_title(
                        self.chat_id, assistant_excerpt="".join(self._pending_text),
                    ))

        elif event.type == THINKING:
            await self._forward(
                {"pump_type": "ws_event", "event": {"type": "thinking", **ed}},
            )
            live = _chat_streaming_state.get(self.chat_id)
            if ed.get("phase") == "start":
                self._thinking_text = ""
                if live:
                    live["thinking_active"] = True
                    live["thinking_text"] = ""
                    live["thinking_tokens"] = 0
            elif ed.get("phase") == "progress":
                # Live-only token-estimate gauge (adaptive-effort models hide
                # the thinking CONTENT — the CLI sends `thinking_tokens` pings
                # instead; see the translator). Never accumulated, never
                # persisted — only the live dict + the forward above.
                if live:
                    live["thinking_active"] = True
                    live["thinking_tokens"] = ed.get("estimated_tokens", 0)
            elif ed.get("text"):
                self._thinking_text += ed["text"]
                if live:
                    live["thinking_text"] = self._thinking_text
            elif ed.get("phase") == "end":
                if self._thinking_text:
                    self._flush_pending_text()
                    thinking_block = self._stamp_speaker(
                        {"type": "thinking", "content": self._thinking_text})
                    self._turn_blocks.append(thinking_block)
                    if live:
                        live["live_blocks"].append(thinking_block)
                self._thinking_text = ""
                if live:
                    live["thinking_active"] = False
                    live["thinking_text"] = ""
                    live["thinking_tokens"] = 0

        elif event.type == TOOL_USE:
            await self._clear_orphan_thinking(live)
            await self._forward(
                {"pump_type": "ws_event", "event": {"type": "tool_start", **ed}},
            )
            tool_name = ed.get("name", "")
            # A memory tool call resets this session's capture-nudge counter
            # (services/memory_nudge). Name shapes: mcp__memory-mcp__memory
            # (CLI/Codex) or bare "memory" (Direct LLM).
            if tool_name == "memory" or tool_name.startswith("mcp__memory-mcp__"):
                try:
                    from services.memory import memory_nudge
                    memory_nudge.record_memory_call(self.session_id)
                except Exception:
                    pass
            # Record tool-start time for any MCP with `outputs` declared so
            # mcp_output_relocation can diff source dir by mtime on
            # TOOL_RESULT and move just the newly-written files.
            if tool_name.startswith("mcp__"):
                try:
                    # mcp__{server_name}__{tool} → resolve server_name to MCP name
                    parts = tool_name.split("__", 2)
                    if len(parts) >= 2:
                        from services.mcp import mcp_output_relocation, mcp_registry
                        server_name = parts[1]
                        for n, m in mcp_registry.get_all_manifests().items():
                            if (m.server_name or m.name) == server_name and m.outputs:
                                mcp_output_relocation.record_tool_start(
                                    self.session_id, n,
                                )
                                break
                except Exception:
                    logger.debug("record_tool_start failed", exc_info=True)
            # Skip tool tracking for Agent/Task/PlanMode (they have dedicated events)
            if tool_name not in self._SKIP_TOOL_PERSIST:
                tool_id = ed.get("tool_id") or tool_name
                self._flush_pending_text()
                tool_block = self._stamp_speaker({
                    "type": "tool", "name": tool_name,
                    "tool_id": tool_id, "summary": "", "active": True,
                    "tool_input": None,
                    "_insert_idx": len(self._turn_blocks),  # track position for correct DB ordering
                })
                self._active_tools[tool_id] = tool_block
                if live:
                    live["active_tools"] = list(self._active_tools.values())
                    live["live_blocks"].append(tool_block)

        elif event.type == TOOL_INPUT:
            await self._forward(
                {"pump_type": "ws_event", "event": {"type": "tool_info", **ed}},
            )
            name = ed.get("name", "")
            summary = ed.get("summary", "")
            tool_input = ed.get("tool_input")
            for t in self._active_tools.values():
                if t["name"] == name:
                    t["summary"] = summary
                    t["tool_input"] = tool_input
                    break
            # Capture plan filename from file-writing tools. Normalize
            # separators first: a Windows-satellite session reports the
            # host-absolute path with backslashes (C:\Users\...\.claude\plans\x.md).
            if name in ("Write", "Edit", "apply_patch", "file_change"):
                fp = (ed.get("file_path", "") or "").replace("\\", "/")
                if ("/.claude/plans/" in fp or "/.codex/plans/" in fp) and fp.endswith(".md"):
                    self._plan_filename = fp.rsplit("/", 1)[-1]
            # Note: TodoWrite todo updates are now handled by the TODO_UPDATE
            # CommonEvent emitted by the translator (cross-layer abstraction).

        elif event.type == TOOL_RESULT:
            tool_id = ed.get("tool_id") or ed.get("name", "")
            tool_name = ed.get("name", "")
            # Codex-style completions carry the output inline (the CLI path
            # gets it via the PostToolUse hook instead): attach it to the
            # still-active block + emit the hook path's `tool_result` frame
            # BEFORE tool_end, and keep the body out of the tool_end frame.
            result_content = ed.pop("result_content", None)
            result_is_error = bool(ed.pop("is_error", False))
            if result_content:
                result_content = truncate_result(result_content)
                summary = result_summary(tool_name, result_content)
                target = self._active_tools.get(tool_id)
                if target is None:
                    for t in self._active_tools.values():
                        if t["name"] == tool_name:
                            target = t
                            break
                if target is not None:
                    target["tool_result"] = result_content
                    target["result_summary"] = summary
                    target["is_error"] = result_is_error
                await self._forward({"pump_type": "ws_event",
                    "event": {"type": "tool_result",
                               "tool_name": tool_name,
                               "tool_use_id": tool_id,
                               "summary": summary,
                               "result_content": result_content}})
            await self._forward(
                {"pump_type": "ws_event", "event": {"type": "tool_end", **ed}},
            )
            tool_evt = self._active_tools.pop(tool_id, None)
            if tool_evt:
                tool_evt["active"] = False  # Also updates live_blocks (same dict ref)
                self._flush_pending_text()
                # Insert at the position where the tool STARTED (not at end).
                # This ensures hook-pushed events (images, previews) that arrived
                # during tool execution appear AFTER the tool block in DB order.
                insert_idx = tool_evt.pop("_insert_idx", len(self._turn_blocks))
                insert_idx = min(insert_idx, len(self._turn_blocks))
                self._turn_blocks.insert(insert_idx, tool_evt)
            if live:
                live["active_tools"] = list(self._active_tools.values())

            # Relocate any files an MCP with `outputs` just produced into
            # the session-scoped workspace subdir. Camoufox screenshots
            # flow through here (privacy + cleanup). For remote sessions
            # the relocated files are also pushed to the satellite so the
            # agent CLI on the satellite can read them at the same path.
            if tool_name.startswith("mcp__"):
                try:
                    parts = tool_name.split("__", 2)
                    if len(parts) >= 2:
                        from services.mcp import mcp_output_relocation, mcp_registry
                        server_name = parts[1]
                        tool_only = parts[2] if len(parts) >= 3 else ""
                        for n, m in mcp_registry.get_all_manifests().items():
                            if (m.server_name or m.name) == server_name and m.outputs:
                                # tool_evt carries the tool's result text (set by
                                # the PostToolUse hook) → precise move-by-filename;
                                # None on the Codex/interactive paths → mtime scan.
                                await mcp_output_relocation.relocate_and_push_for_tool(
                                    self.session_id, n, tool_only,
                                    result_text=(tool_evt or {}).get("tool_result"),
                                )
                                break
                except Exception:
                    logger.debug("output relocation failed", exc_info=True)

            # Generic per-tool cost evaluation. Looks up the manifest's
            # `costs` block (if any) and applies the first matching rule
            # against the args we stashed when we saw the TOOL_INPUT event.
            # See services/mcp/mcp_cost_engine.py.
            #
            # Caveats:
            #   - Failed tool calls are NOT charged: the PostToolUse hook
            #     reports `is_error` (the structured MCP flag, or an "Error…"
            #     result for MCPs that return failures as plain text), which we
            #     stashed on the tool block above. The direct/codex paths have
            #     no such hook, so a failed tool there can still be charged
            #     (secondary follow-up).
            #   - Aborts between TOOL_USE and TOOL_RESULT are NOT charged
            #     (TOOL_RESULT never fires).
            if (
                tool_evt is not None
                and not tool_evt.get("is_error")
                and tool_name.startswith("mcp__")
            ):
                try:
                    from services.mcp import mcp_cost_engine
                    found = mcp_cost_engine.find_costs_block_for_tool(tool_name)
                    if found is not None:
                        mcp_name, plain_tool, costs_block = found
                        hit = mcp_cost_engine.evaluate(
                            mcp_name, plain_tool, tool_evt.get("tool_input"), costs_block,
                        )
                        if hit is not None and hit.amount > 0:
                            self._total_cost_delta += hit.amount
                            key = (hit.provider, hit.model)
                            self._mcp_cost_by_key[key] = round(
                                self._mcp_cost_by_key.get(key, 0.0) + hit.amount, 6,
                            )
                            await self._forward({"pump_type": "ws_event", "event": {
                                "type": "mcp_cost",
                                "cost_usd": hit.amount,
                                "provider": hit.provider,
                                "model": hit.model,
                                "tool": plain_tool,
                                "mcp": mcp_name,
                            }})
                except Exception:
                    logger.exception("mcp cost evaluation failed for %s", tool_name)

        elif event.type == SUBAGENT_START:
            await self._forward(
                {"pump_type": "ws_event", "event": {"type": "task_spawn", **ed}},
            )
            self._flush_pending_text()
            self._turn_blocks.append({"type": "task_spawn", **ed})
            is_bg = ed.get("run_in_background", False)
            if live:
                agent_block = {
                    "type": "agent",
                    "description": ed.get("description", ""),
                    "subagent_type": ed.get("subagent_type", ""),
                    "background": is_bg,
                    "tool_use_id": ed.get("tool_use_id", ""),  # completion key (SUBAGENT_END)
                    "active": True,
                    # Full Agent tool input — reconnect renders the same
                    # expandable detail as the live pill.
                    "tool_input": ed.get("tool_input"),
                }
                live["active_agents"].append(agent_block)
                live["live_blocks"].append(agent_block)  # Same dict ref — updates propagate

        elif event.type == SUBAGENT_END:
            # Deterministic per-agent completion (stdout task_notification
            # backup path; the SubagentStop hook delivers the same WS event
            # out-of-band via push_pump_event). Clears the widget by id —
            # order-independent, no FIFO.
            tuid = ed.get("tool_use_id", "")
            await self._forward(
                {"pump_type": "ws_event",
                 "event": {"type": "bg_agent_done", "tool_use_id": tuid}},
            )
            if live and tuid:
                for a in live["active_agents"]:
                    if a.get("tool_use_id") == tuid:
                        a["active"] = False
                        break

        elif event.type == BG_COMMAND_START:
            # Backgrounded bash command — mirror SUBAGENT_START: a live badge +
            # inline block keyed by the Bash tool_use_id. The dashboard FOLDS
            # the command's normal tool card into this block by that id
            # (pairBgCommandBlocks) — one expandable pill per command.
            # Completion (BG_COMMAND_END) clears it by id, order-independent
            # (multiple bg commands run concurrently).
            await self._forward(
                {"pump_type": "ws_event", "event": {"type": "bg_command_spawn", **ed}},
            )
            self._flush_pending_text()
            self._turn_blocks.append({"type": "bg_command_spawn", **ed})
            if live:
                command_block = {
                    "type": "command",
                    "command": ed.get("command", ""),
                    "description": ed.get("description", ""),
                    "tool_use_id": ed.get("tool_use_id", ""),  # completion key (BG_COMMAND_END)
                    "active": True,
                }
                live["active_commands"].append(command_block)
                live["live_blocks"].append(command_block)  # same dict ref — updates propagate

        elif event.type == BG_COMMAND_END:
            # Deterministic per-command completion — driven by the CLI
            # task_updated frame (no completion hook exists for bg bash). Clears
            # the widget by tool_use_id, order-independent (no FIFO guessing).
            tuid = ed.get("tool_use_id", "")
            await self._forward(
                {"pump_type": "ws_event",
                 "event": {"type": "bg_command_done", "tool_use_id": tuid,
                           "status": ed.get("status", "")}},
            )
            if live and tuid:
                for c in live["active_commands"]:
                    if c.get("tool_use_id") == tuid:
                        c["active"] = False
                        break

        elif event.type == WORKFLOW_START:
            # Dynamic workflow (Opus 4.8 Workflow tool) — one orchestration
            # spawning many agents, surfaced as a live phase/agent tree in the
            # floating WorkflowPanel. Like the TodoPanel checklist, this is
            # live-state only (not persisted as a turn block) — the agents'
            # actual output streams as normal text/tool blocks.
            tuid = ed.get("tool_use_id", "")
            await self._forward({"pump_type": "ws_event", "event": {
                "type": "workflow_start", "tool_use_id": tuid,
                "workflow_name": ed.get("workflow_name", ""),
            }})
            if live:
                live["workflows"][tuid] = {
                    "tool_use_id": tuid,
                    "workflow_name": ed.get("workflow_name", ""),
                    "progress": [], "active": True,
                }

        elif event.type == WORKFLOW_PROGRESS:
            tuid = ed.get("tool_use_id", "")
            progress = ed.get("workflow_progress", [])
            # Live state always holds the latest snapshot (reconnect accuracy).
            if live and tuid in live.get("workflows", {}):
                live["workflows"][tuid]["progress"] = progress
            # Coalesce WS forwards so a fast workflow can't flood the satellite.
            now = time.monotonic()
            if now - self._wf_last_forward.get(tuid, 0.0) >= _WORKFLOW_PROGRESS_MIN_INTERVAL:
                self._wf_last_forward[tuid] = now
                await self._forward({"pump_type": "ws_event", "event": {
                    "type": "workflow_progress", "tool_use_id": tuid,
                    "workflow_progress": progress,
                }})

        elif event.type == WORKFLOW_END:
            tuid = ed.get("tool_use_id", "")
            self._wf_last_forward.pop(tuid, None)
            await self._forward({"pump_type": "ws_event", "event": {
                "type": "workflow_end", "tool_use_id": tuid,
            }})
            if live and tuid in live.get("workflows", {}):
                live["workflows"][tuid]["active"] = False

        elif event.type == DELEGATE_SPAWN:
            evt = {"type": "delegate_spawn", **ed}
            await self._forward({"pump_type": "ws_event", "event": evt})
            self._flush_pending_text()
            self._turn_blocks.append(evt)
            if live:
                delegate_block = {
                    "type": "delegate",
                    "task_id": ed.get("task_id", ""),
                    "task_name": ed.get("task_name", ""),
                    "agent": ed.get("agent", ""),
                    # Full prompt for the expandable pill (reconnect parity).
                    "prompt": ed.get("prompt", ""),
                    "active": True,
                    "status": "running",  # set to completed/failed/cancelled on terminal
                }
                live["active_delegates"].append(delegate_block)
                live["live_blocks"].append(delegate_block)

        elif event.type == PERMISSION_REQUEST:
            self._flush_pending_text()
            self._turn_blocks.append({"type": "permission_prompt", **ed})
            # Gate: use the same queue as perm_queue permissions
            perm_data = {**ed, "event_type": "permission_prompt"}
            await self._queue_or_show_permission(perm_data)

        elif event.type == PLAN_MODE:
            evt = {"type": "plan_mode", **ed}
            await self._forward(
                {"pump_type": "ws_event", "event": evt},
            )
            self._flush_pending_text()
            self._turn_blocks.append(evt)
            if live:
                live["live_blocks"].append(evt)
            # Update hook mode + DB so plan mode is enforced even without WS
            action = ed.get("action", "")
            if action == "enter":
                set_session_mode(self.session_id, "plan")
                if self.chat_id:
                    task_store.update_chat(self.chat_id, permission_mode="plan")
            # exit is handled by plan_review_response (implement sets acceptEdits/default)

        elif event.type == TODO_UPDATE:
            # Cross-layer todo/checklist update (TodoWrite for CLI, update_plan for Codex)
            todos = ed.get("todos", [])
            self._current_todos = todos
            await self._forward(
                {"pump_type": "ws_event", "event": {"type": "todo_update", "todos": todos}},
            )
            if live:
                live["todos"] = todos
            # Some checklist sources have no tool-path block the panel can rehydrate
            # from on reload — Codex's update_plan (no block at all) and the CLI
            # TaskCreate/TaskUpdate family (its own inline cards, but no TodoWrite
            # snapshot). Persist a TodoWrite-shaped block so the checklist survives
            # turn end + refresh — one block per turn-segment, updated in place (the
            # frontend + get_last_todo_snapshot restore the panel from the last one).
            # panel_only marks the Task-tool case so the frontend suppresses it inline
            # (the TaskCreate/TaskUpdate cards already render); codex omits it (the
            # synthesized block IS its only inline representation).
            if ed.get("persist_block"):
                if self._todo_block is None:
                    self._flush_pending_text()
                    self._todo_block = self._stamp_speaker({
                        "type": "tool", "name": "TodoWrite",
                        "tool_input": {"todos": todos},
                    })
                    if ed.get("panel_only"):
                        self._todo_block["panel_only"] = True
                    self._turn_blocks.append(self._todo_block)
                else:
                    self._todo_block["tool_input"]["todos"] = todos

        elif event.type == GOAL_UPDATE:
            # Codex thread goal — chat-durable panel state, NOT a turn block
            # (like workflows). Reloads restore it from chats.thread_goal via
            # restore.goal; live.goal only covers a mid-turn reconnect. The DB
            # write is change-gated: goal notifications can repeat every turn
            # and each update_chat bumps updated_at.
            goal = goal_payload_to_state(ed)
            changed = goal != self._current_goal
            self._current_goal = goal
            await self._forward(
                {"pump_type": "ws_event", "event": {"type": "goal_update", "goal": goal}},
            )
            if live:
                live["goal"] = goal
            if changed and self.chat_id:
                task_store.update_chat(
                    self.chat_id, thread_goal=json.dumps(goal) if goal else None)

        elif event.type == CONTEXT_COMPACT:
            # Context compression event (CLI auto-compact, Codex compaction, etc.)
            phase = ed.get("phase", "")
            evt = {"type": "context_compact", **ed}
            await self._forward({"pump_type": "ws_event", "event": evt})
            if phase == "completed":
                self._flush_pending_text()
                self._turn_blocks.append(evt)
                if live:
                    live["live_blocks"].append(evt)

        elif event.type == SYSTEM:
            subtype = ed.get("subtype", "")
            # Claude CLI subagent completion (fg + bg) is now a first-class
            # SUBAGENT_END event keyed by tool_use_id (SubagentStop hook +
            # task_notification backup) — no FIFO matching, no message_start
            # inference. `fg_agents_complete` is still emitted by the Codex
            # layer (collab `wait` tool), which has no per-agent completion
            # signal, so that branch stays for Codex.
            if subtype == "fg_agents_complete":
                await self._forward(
                    {"pump_type": "ws_event", "event": {"type": "fg_agents_complete"}},
                )
                self._flush_pending_text()
                self._turn_blocks.append({"type": "fg_agents_complete"})
                if live:
                    for a in live["active_agents"]:
                        if not a.get("background") and a.get("active", True):
                            a["active"] = False
            elif subtype == "meeting_started":
                evt = {"type": "system", **ed}
                await self._forward({"pump_type": "ws_event", "event": evt})
                self._flush_pending_text()
                self._turn_blocks.append(evt)
                # Save immediately so it appears before any QUEUE_TURN user messages
                self._save_turn_blocks()
                if live:
                    live["meeting_participants"] = ed.get("participants", [])
                    # Don't re-add to live_blocks — it's now in DB and will come
                    # via chat_history on reconnect.  Re-adding caused duplicates.
            elif subtype == "meeting_turn_start":
                self._meeting_agent = ed.get("agent", "")
                evt = {"type": "system", **ed}
                await self._forward({"pump_type": "ws_event", "event": evt})
                self._flush_pending_text()
                self._turn_blocks.append(evt)
                if live:
                    live["meeting_agent"] = self._meeting_agent
                    live["live_blocks"].append(evt)
            elif subtype == "meeting_failed":
                # Pre-turn meeting failure (admission denial, spawn failure…)
                # surfaced by the orchestrator: persist immediately so the
                # attach-time chat_history re-send carries the banner, and
                # clear live meeting state so the pill goes away. Not re-added
                # to live_blocks after the save (meeting_started precedent —
                # it would duplicate on reconnect).
                self._meeting_agent = None
                evt = {"type": "system", **ed}
                await self._forward({"pump_type": "ws_event", "event": evt})
                self._flush_pending_text()
                self._turn_blocks.append(evt)
                self._save_turn_blocks()
                if live:
                    live["meeting_agent"] = None
                    live["meeting_participants"] = []
            elif subtype in ("meeting_turn_end", "meeting_concluded", "meeting_agent_failed"):
                if subtype in ("meeting_turn_end", "meeting_concluded"):
                    self._meeting_agent = None
                evt = {"type": "system", **ed}
                await self._forward({"pump_type": "ws_event", "event": evt})
                self._flush_pending_text()
                self._turn_blocks.append(evt)
                # Meeting costs now arrive as separate METADATA events with
                # _meeting_cost=True (emitted by meeting_produce after computing
                # per-turn delta). No cost extraction from turn_end/concluded.
                # Save each meeting turn's blocks immediately so DB order
                # matches chronological order (user QUEUE_TURN messages also
                # save immediately, and would otherwise appear before all
                # meeting content which is deferred to PRODUCER_DONE).
                self._save_turn_blocks()
                if live:
                    if subtype in ("meeting_turn_end", "meeting_concluded"):
                        live["meeting_agent"] = None
                    if subtype == "meeting_concluded":
                        live["meeting_participants"] = []
                    live["live_blocks"].append(evt)
            elif subtype not in ("task_started", "task_progress"):
                evt = {"type": "system", **ed}
                await self._forward(
                    {"pump_type": "ws_event", "event": evt},
                )
                self._flush_pending_text()
                self._turn_blocks.append(evt)
                if live:
                    live["live_blocks"].append(evt)

        elif event.type == METADATA:
            # Persist Codex thread_id for resume after proxy restart
            codex_tid = ed.get("codex_thread_id")
            if codex_tid and self.chat_id:
                task_store.update_chat(self.chat_id, codex_thread_id=codex_tid)
                return  # internal metadata — don't forward to WS or save as turn block

            is_delta = ed.get("cost_is_delta") or ed.get("_meeting_cost")
            if is_delta:
                # Cost is already a per-turn delta (Direct LLM, Codex, meetings).
                # Use directly — don't touch cumulative tracking.
                turn_cost = ed.get("cost_usd", 0)
                meta_event = {"type": "metadata", "cost_usd": turn_cost}
                # Preserve context/cache/duration fields if present
                for k in ("context_used", "context_max", "cache_read",
                          "cache_write", "input_tokens", "output_tokens",
                          "duration_ms"):
                    if k in ed:
                        meta_event[k] = ed[k]
                await self._forward({"pump_type": "ws_event", "event": meta_event})
                self._flush_pending_text()
                self._turn_blocks.append(meta_event)
                self._total_cost_delta += turn_cost
                self._llm_cost_delta += turn_cost
            else:
                # Cost is CUMULATIVE session total (CLI). Compute per-turn delta.
                session_cost = ed.get("cost_usd", 0)
                if session_cost < self._last_session_cost:
                    # CLI process restarted — cost counter reset
                    self._last_session_cost = 0.0
                turn_cost = max(0, session_cost - self._last_session_cost)
                self._last_session_cost = session_cost
                _session_cumulative_cost[self.session_id] = session_cost
                # Forward per-turn cost (not cumulative)
                meta_event = {**ed, "cost_usd": turn_cost}
                await self._forward(
                    {"pump_type": "ws_event", "event": {"type": "metadata", **meta_event}},
                )
                self._flush_pending_text()
                self._turn_blocks.append({"type": "metadata", **meta_event})
                self._total_cost_delta += turn_cost
                self._llm_cost_delta += turn_cost
            # Track context + cache stats for persistence
            ctx_used = ed.get("context_used", 0)
            ctx_max = ed.get("context_max", 0)
            if ctx_used > 0 and ctx_max > 0:
                self._context_used = ctx_used
                self._context_max = ctx_max
            it = ed.get("input_tokens", 0)
            cr = ed.get("cache_read", 0)
            cw = ed.get("cache_write", 0)
            ot = ed.get("output_tokens", 0)
            if it > 0:
                self._input_tokens = it
            if ot > 0:
                self._output_tokens = ot
            if cr > 0 or cw > 0:
                self._cache_read = cr
                self._cache_write = cw

        elif event.type == ERROR:
            # NOTE: the main `_run` loop intercepts ERROR (forward + break) before
            # this dispatcher is reached, so a grace-expiry's durable marker is
            # persisted there (see the ERROR branch in `_run`). This path remains
            # for any future caller that routes an ERROR through `_process_event`.
            await self._forward(
                {"pump_type": "ws_event", "event": {"type": "error", "message": ed.get("message", "")}},
            )

        elif event.type == DONE:
            # Turn boundary — flush all pending data to DB in order
            self._flush_pending_text()
            await self._flush_pending_previews()
            for tool_evt in self._active_tools.values():
                self._turn_blocks.append(tool_evt)
            self._active_tools.clear()
            self._save_turn_blocks()
            self._pending_text.clear()
            if live:
                # Don't reset live_blocks — keep accumulating for live_state
                # reconnect (DB messages are truncated at _db_msg_cutoff_id)
                self._live_append_text(live, "\n\n")  # paragraph break between turns
                live["active_tools"] = []
            await self._forward({"pump_type": "is_done"})

    async def _handle_perm_event(self, perm_data: dict):
        """Handle a permission queue event — forward to WS or store for re-presentation."""
        evt_type = perm_data.get("event_type", "")
        live = _chat_streaming_state.get(self.chat_id)

        if evt_type == "images":
            # Unified gallery event (1-N images). Replaces the old singular
            # `image` event — display-mcp, image-gen-mcp, file-tools-mcp, and
            # the new image-search-mcp all post via /v1/hooks/images now.
            evt = self._stamp_speaker(artifact_event_from_perm_item(perm_data))
            self._flush_pending_text()
            # Remove image_generating placeholder (if any) — applies when the
            # gallery is coming from image-gen-mcp's completion path.
            for i in range(len(self._turn_blocks) - 1, -1, -1):
                if self._turn_blocks[i].get("type") == "image_generating":
                    self._turn_blocks.pop(i)
                    break
            self._turn_blocks.append(evt)
            await self._forward({"pump_type": "ws_event", "event": evt})
            if live:
                for i in range(len(live["live_blocks"]) - 1, -1, -1):
                    if live["live_blocks"][i].get("type") == "image_generating":
                        live["live_blocks"].pop(i)
                        break
                live["live_blocks"].append(evt)
        elif evt_type == "image_generating":
            evt = self._stamp_speaker(artifact_event_from_perm_item(perm_data))
            self._flush_pending_text()
            self._turn_blocks.append(evt)
            await self._forward({"pump_type": "ws_event", "event": evt})
            if live:
                live["live_blocks"].append(evt)
        elif evt_type == "image_gen_failed":
            await self._forward({"pump_type": "ws_event",
                                 "event": artifact_event_from_perm_item(perm_data)})
            # Remove placeholder from turn_blocks and live_blocks
            for i in range(len(self._turn_blocks) - 1, -1, -1):
                if self._turn_blocks[i].get("type") == "image_generating":
                    self._turn_blocks.pop(i)
                    break
            if live:
                for i in range(len(live["live_blocks"]) - 1, -1, -1):
                    if live["live_blocks"][i].get("type") == "image_generating":
                        live["live_blocks"].pop(i)
                        break
        elif evt_type == "url":
            evt = self._stamp_speaker(artifact_event_from_perm_item(perm_data))
            self._flush_pending_text()
            self._turn_blocks.append(evt)
            await self._forward({"pump_type": "ws_event", "event": evt})
            if live:
                live["live_blocks"].append(evt)
        elif evt_type == "file":
            evt = self._stamp_speaker(artifact_event_from_perm_item(perm_data))
            self._flush_pending_text()
            self._turn_blocks.append(evt)
            await self._forward({"pump_type": "ws_event", "event": evt})
            if live:
                live["live_blocks"].append(evt)
        elif evt_type == "ui":
            evt = self._stamp_speaker(artifact_event_from_perm_item(perm_data))
            self._flush_pending_text()
            self._turn_blocks.append(evt)
            await self._forward({"pump_type": "ws_event", "event": evt})
            if live:
                live["live_blocks"].append(evt)
        elif evt_type in ("video", "audio"):
            # Audio/video player block. Carries either a web URL (src_kind=url)
            # or a capability token (src_kind=token → /v1/media/{token}); the
            # bytes are NEVER inlined (unlike images). Replaces a
            # media_processing placeholder if one was shown during transcode.
            evt = self._stamp_speaker(artifact_event_from_perm_item(perm_data))
            self._flush_pending_text()
            for i in range(len(self._turn_blocks) - 1, -1, -1):
                if self._turn_blocks[i].get("type") == "media_processing":
                    self._turn_blocks.pop(i)
                    break
            self._turn_blocks.append(evt)
            await self._forward({"pump_type": "ws_event", "event": evt})
            if live:
                for i in range(len(live["live_blocks"]) - 1, -1, -1):
                    if live["live_blocks"][i].get("type") == "media_processing":
                        live["live_blocks"].pop(i)
                        break
                live["live_blocks"].append(evt)
        elif evt_type == "media_processing":
            # Transient skeleton shown while the proxy transcodes a non-web-safe
            # codec. Removed when the real video/audio block arrives or
            # on media_failed; not persisted (skipped in _save_turn_blocks).
            evt = artifact_event_from_perm_item(perm_data)
            self._flush_pending_text()
            self._turn_blocks.append(evt)
            await self._forward({"pump_type": "ws_event", "event": evt})
            if live:
                live["live_blocks"].append(evt)
        elif evt_type == "media_failed":
            await self._forward({"pump_type": "ws_event",
                                 "event": artifact_event_from_perm_item(perm_data)})
            for i in range(len(self._turn_blocks) - 1, -1, -1):
                if self._turn_blocks[i].get("type") == "media_processing":
                    self._turn_blocks.pop(i)
                    break
            if live:
                for i in range(len(live["live_blocks"]) - 1, -1, -1):
                    if live["live_blocks"][i].get("type") == "media_processing":
                        live["live_blocks"].pop(i)
                        break
        elif evt_type == "document_preview":
            evt = self._stamp_speaker(artifact_event_from_perm_item(perm_data))
            # Buffer preview — only forward to dashboard at turn end.
            # Replace-in-place in turn_blocks and live_blocks for state consistency.
            self._flush_pending_text()
            replaced = False
            for i, block in enumerate(self._turn_blocks):
                if (block.get("type") == "document_preview"
                        and block.get("file_id") == evt["file_id"]):
                    # Intra-turn supersede: the replaced push's snapshot was
                    # never exposed to the dashboard (previews only forward at
                    # flush) — drop its file now.
                    old_snap = block.get("snapshot_id") or ""
                    if old_snap and old_snap != evt.get("snapshot_id"):
                        from services.media import preview_snapshots
                        preview_snapshots.delete_snapshot(self.chat_id, old_snap)
                    self._turn_blocks[i] = evt
                    replaced = True
                    break
            if not replaced:
                self._turn_blocks.append(evt)
            # Buffer latest preview per file_id (don't forward WS event yet)
            self._pending_previews[evt["file_id"]] = evt
            if live:
                replaced_live = False
                for i, lb in enumerate(live["live_blocks"]):
                    if (lb.get("type") == "document_preview"
                            and lb.get("file_id") == evt["file_id"]):
                        live["live_blocks"][i] = evt
                        replaced_live = True
                        break
                if not replaced_live:
                    live["live_blocks"].append(evt)
        elif evt_type == "tool_result":
            tool_name = perm_data["tool_name"]
            result_content = perm_data.get("result_content", "")
            tool_use_id = perm_data.get("tool_use_id", "") or ""
            await self._forward({"pump_type": "ws_event",
                "event": {"type": "tool_result",
                           "tool_name": tool_name,
                           "tool_use_id": tool_use_id,
                           "summary": perm_data["summary"],
                           "result_content": result_content}})
            # Attach result to the active tool block (PostToolUse fires before tool_end,
            # so the tool is still in _active_tools). This ensures tool_result is
            # persisted to DB when the tool block moves to _turn_blocks on tool_end.
            # Prefer the exact tool_use_id (parallel same-name tools attach to
            # the right block); fall back to name for older CLIs without it.
            target = self._active_tools.get(tool_use_id) if tool_use_id else None
            if target is None:
                for t in self._active_tools.values():
                    if t["name"] == tool_name:
                        target = t
                        break
            if target is not None:
                target["tool_result"] = result_content
                target["result_summary"] = perm_data["summary"]
                # Carried to the cost engine at TOOL_RESULT so a failed
                # tool call (e.g. a failed image generation) isn't charged.
                target["is_error"] = bool(perm_data.get("is_error", False))
            elif tool_name in ("Agent", "Task") and tool_use_id:
                # A FOREGROUND subagent's final report (Agent tools are not
                # tracked in _active_tools — they have dedicated task_spawn
                # blocks). Attach it to the spawn block so the dashboard's
                # subagent pill can expand to the report; same dict ref is in
                # _turn_blocks (persists at DONE) and live_blocks (reconnect).
                # Background spawns are skipped — their PostToolUse result is
                # just the "launched" ack, and the real report arrives via
                # task_notification in a later turn.
                for blk in reversed(self._turn_blocks):
                    if (blk.get("type") == "task_spawn"
                            and blk.get("tool_use_id") == tool_use_id):
                        if not blk.get("run_in_background"):
                            blk["tool_result"] = result_content
                        break
                if live:
                    for a in live.get("active_agents", []):
                        if (a.get("tool_use_id") == tool_use_id
                                and not a.get("background")):
                            a["tool_result"] = result_content
                            break
        elif evt_type == "question":
            evt = self._stamp_speaker(
                {"type": "question", "tool_name": perm_data["tool_name"],
                 "tool_input": perm_data["tool_input"]})
            self._flush_pending_text()
            self._turn_blocks.append(evt)
            await self._forward({"pump_type": "ws_event", "event": evt})
            if live:
                live["live_blocks"].append(evt)
        elif evt_type == "permission_prompt":
            # Gate: only show one blocking prompt at a time
            await self._queue_or_show_permission(perm_data)
        elif evt_type == "plan_review":
            plan_content = perm_data.get("plan", "")
            plan_tool_input = perm_data.get("tool_input", {})
            plan_filename = ""
            for key in ("planFilePath", "plan_file_path", "file_path"):
                val = plan_tool_input.get(key, "")
                if val:
                    plan_filename = val.rsplit("/", 1)[-1]
                    break
            if not plan_filename:
                # Reuse plan filename within the same chat to avoid duplicates
                # when the user edits the plan (same CLI plan file, new request_id)
                if self._plan_filename:
                    plan_filename = self._plan_filename
                else:
                    plan_filename = f"plan-{perm_data['request_id'][:8]}.md"
            self._plan_filename = plan_filename
            if self.chat_id and plan_content:
                task_store.add_chat_plan(self.chat_id, plan_filename, plan_content)
            enriched = {**perm_data, "filename": plan_filename}
            # Save plan_review event for DB history reconstruction
            self._flush_pending_text()
            self._turn_blocks.append({
                "type": "plan_review", "request_id": perm_data.get("request_id", ""),
                "plan": plan_content, "tool_input": plan_tool_input,
                "filename": plan_filename,
            })
            # Gate: only show one blocking prompt at a time
            await self._queue_or_show_permission(enriched)
        elif evt_type == "mode_restored":
            mode = perm_data.get("mode", "default")
            task_store.update_chat(self.chat_id, permission_mode=mode)
            await self._forward({"pump_type": "perm_mode_restored", "mode": mode})


# The background-work monitors live in pump_bg_monitors.py; re-exported here
# so `from core.events.stream_pump import bg_monitor_running` (ws/dashboard) works.
from core.events.pump_bg_monitors import (  # noqa: F401
    bg_monitor_running,
    _bg_agent_monitor,
    bg_command_monitor_running,
    _bg_command_monitor,
)
