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
import contextlib
import functools
import json
import logging
import time
from datetime import datetime, timezone

from storage import database as task_store
from storage.automation import run_status
from storage.pg import run_db
from core.events import chat_writer, tool_roles
from services.notifications import notification_manager
from core.events import artifact_events
from core.events.artifact_events import artifact_event_from_perm_item
from ws import chat_phase
from ws import wire_events as wire

#: The platform's own delegate MCP tool: its call renders as the delegate
#: pill from the proxy's ``delegate_spawn`` frame, never as a tool block
#: (the dashboard's skip set carries the same name — ``lib/tools/roles.ts``).
DELEGATE_TOOL = "mcp__delegation-mcp__delegate"
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
    CHECK_VERDICT, BG_COMMAND_START, BG_COMMAND_END,
    WORKFLOW_START, WORKFLOW_PROGRESS, WORKFLOW_END,
    PLAN_MODE, SYSTEM, METADATA, DONE, ERROR, QUEUE_TURN, ARTIFACT_TURN,
    PRODUCER_DONE, TODO_UPDATE, GOAL_UPDATE, CONTEXT_COMPACT,
    TurnInput, goal_payload_to_state,
)

# Min seconds between workflow_progress WS forwards per workflow (the live
# tree is re-sent every tick; coalesce so a busy workflow can't flood the
# satellite WS send-queue / frame cap). The terminal
# frame and WORKFLOW_END always go through.
_WORKFLOW_PROGRESS_MIN_INTERVAL = 0.3

# LLM chat-title early-fire thresholds — canonical values live in
# services/title_generator.py (shared with the interactive funnel). Chars:
# fire once the first response crosses the length threshold. Tools: a
# tool-heavy first turn (agentic work, little prose) can run for minutes
# without crossing it — fire at the tool-call count instead; subagent/
# delegate spawns count too (they equally signal a long working turn), and
# the fire is effectively prompt-only-titled (pending text flushes per
# event; no assistant rows are persisted mid-turn) — intended cap behavior.
from services.title_generator import (
    TITLE_CHAR_THRESHOLD as _TITLE_CHAR_THRESHOLD,
    TITLE_TOOL_THRESHOLD as _TITLE_TOOL_THRESHOLD,
)
from core.session import session_kind

logger = logging.getLogger("claude-proxy")

# Text and thinking deltas coalesce per pump: a delta goes out at once when
# none went out in the last window, else it joins one pending frame flushed
# at the window's end (any other frame flushes it first). A token-by-token
# engine then costs at most one delta frame per window per viewer.
_DELTA_FLUSH_S = 0.04
# A dashboard viewer's queue is cut here (about 1 MB of frames): cleared, one
# PUMP_RESYNC, nothing after it, so a stalled viewer cannot grow memory.
_VIEWER_QUEUE_MAX = 2048
# Pump-local item (not a wire type): the viewer fell behind and must resync
# through a resume (chat_history, then a fresh live_state).
PUMP_RESYNC = "resync"


class _ViewerQueue(asyncio.Queue):
    """One subscriber's queue. Only ``bounded`` ones (dashboard viewers) are
    cut at ``_VIEWER_QUEUE_MAX``; phone and duplex keep every frame (their
    consumers speak the text)."""

    def __init__(self, bounded: bool) -> None:
        super().__init__()
        self.bounded = bounded
        self.overflowed = False


def _delta_key(event_type: str, payload: dict) -> tuple | None:
    """What a delta may merge with, or None when the frame never coalesces:
    a TEXT that is only content, a THINKING text delta (same other keys), a
    THINKING progress ping (latest wins)."""
    if event_type == TEXT:
        return (TEXT,) if payload.keys() == {"content"} else None
    if event_type == THINKING:
        phase = payload.get("phase")
        if phase == "progress":
            return ("progress",) if payload.keys() == {"phase", "estimated_tokens"} else None
        if payload.get("text") and phase not in ("start", "end"):
            others = dict(payload)
            others.pop("text")
            return (THINKING, json.dumps(others, sort_keys=True, default=str))
    return None


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


def _evict_latest(blocks: list[dict], kind: str) -> None:
    """Remove the LATEST block of ``kind`` (a placeholder the real artifact
    or its failure replaces) from a block list, in place."""
    for i in range(len(blocks) - 1, -1, -1):
        if blocks[i].get("type") == kind:
            blocks.pop(i)
            return


def _serialize_turn_rows(blocks: list[dict]) -> list[tuple[str, str, str, str]]:
    """``(role, content, event_type, event_data)`` rows for the writer, built
    ON THE LOOP: the block dicts are shared with live_state and keep changing
    afterwards (a plan_review action, badge flags), and json.dumps on a worker
    thread while the loop mutates one would raise mid-insert. The meeting
    badge lookup is an agent_store cache read."""
    rows: list[tuple[str, str, str, str]] = []
    for block in blocks:
        artifact = artifact_events.kind_of(block["type"])
        if artifact is not None and not artifact.saved:
            # A transient transcode skeleton — never persisted. If a turn
            # ends while a transcode is still running, the placeholder is
            # dropped rather than frozen into history (the table says which
            # kinds; every non-artifact block is a row).
            continue
        if block["type"] == wire.LIVE_TEXT:
            meeting_agent = block.get("_meeting_agent")
            event_data = ""
            if meeting_agent:
                from storage.agents import agent_store
                ad = agent_store.get_agent(meeting_agent)
                event_data = json.dumps({
                    "agent_slug": meeting_agent,
                    "agent_display_name": (ad or {}).get("display_name", meeting_agent),
                    "agent_color": (ad or {}).get("color", ""),
                    "badge": "meeting",
                })
            rows.append(("assistant", block["content"], "", event_data))
        else:
            rows.append(("event", "", block["type"], json.dumps(block)))
    return rows


def _record_usage(chat_row: dict, snap: dict) -> None:
    """Record one usage_records row for the LLM + one per (provider, model)
    for any per-tool MCP costs accumulated this turn — from the cost job, on
    the DB executor, with the pump's values snapshotted on the loop.

    The LLM row is always written (even at $0 cost) so token counts and the
    message_count survive — analytics depends on them. MCP rows are only
    written when their cost is positive.
    """
    try:
        from services.billing import usage_service
        from config import get_model_provider

        chat_model = chat_row.get("model", "")
        llm_provider = get_model_provider(chat_model) or "anthropic"
        llm_cost = max(0.0, round(snap["llm_cost_delta"], 6))

        common = {
            "user_sub": chat_row.get("user_sub"),
            "agent": chat_row.get("agent", ""),
            "scope": snap["scope"],
            "source_type": snap["source_type"],
            "source_id": snap["chat_id"],
        }
        # Agent scope must mean platform-paid. A Shared-only agent's human
        # chat mounts agent scope but runs on the interacting USER's
        # subscription (the chat row's owner is the synthetic agent::{slug},
        # not a person) — bill it to the payer so it lands in their Usage
        # tab, not the agent bucket. Platform-paid sources (tasks, phone,
        # meetings, service sessions) acquire agent-scope and have no payer,
        # so they are untouched.
        if snap["scope"] == "agent" and snap["payer"]:
            common["user_sub"] = snap["payer"]
            common["scope"] = "user"

        rows: list[dict] = [{
            **common,
            "cost_usd": llm_cost,
            "input_tokens": snap["input_tokens"],
            "output_tokens": snap["output_tokens"],
            "cache_read": snap["cache_read"],
            "cache_write": snap["cache_write"],
            "message_count": 1,
            "provider": llm_provider,
            "model": chat_model,
            "source_key": snap["sub_id"],
        }]
        for (provider, model), cost in snap["mcp_cost_by_key"].items():
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
        chat_owner: str = "",
        chat_agent: str = "",
    ):
        self.chat_id = chat_id
        self.session_id = session_id
        self.producer = producer
        self.event_queue = event_queue
        self.perm_queue = perm_queue
        self.implementing_plan = implementing_plan
        self.scope = scope
        self.source_type = source_type
        # The chat row's owner + agent when the caller already holds them
        # (dashboard turns): the turn-start "streaming" broadcast then fires
        # synchronously in _run, in the frame order viewers expect; other
        # producers get it from the start job's result.
        self._chat_owner = chat_owner
        self._chat_agent = chat_agent

        self._ws_queues: list[_ViewerQueue] = []
        self._done = False
        # PUMP_ENDED went out: a later attach() gets it at once.
        self._ended = False
        self._live_ready = False  # _init_live_state ran (start() or _run)
        # Delta coalescing (_forward_delta): the pending frame, its merge key,
        # when a delta frame last went out, the flush timer.
        self._delta_item: dict | None = None
        self._delta_key: tuple | None = None
        self._delta_sent_at = float("-inf")
        self._delta_timer: asyncio.TimerHandle | None = None
        self._abort_requested = False  # abort() sets; drives the resumed-task run-status close
        self._task: asyncio.Task | None = None
        # Terminal engine/provider error, if the stream died on one. The
        # event loop BREAKS on ERROR, so a non-empty value here is terminal
        # by construction. Task runs consult it (scheduler step 7) so an
        # engine failure ("Not logged in", a cross-engine model 400) never
        # lands as a deceptive `completed` run with empty output.
        self.last_error: str = ""

        # Message queue (shared with producer closure)
        self.message_queue: list[TurnInput] = []
        # System prompt queue — delivered silently (no user bubble).
        # Used for delegate results and bg nudges during background drain.
        self.system_queue: list[str] = []
        # True only when a producer actually DRAINS system_queue (dashboard
        # turn loop, duplex, meetings). Task/scheduler pumps never do — the
        # delivery ladder must not "succeed" onto a queue nobody reads
        # (queue_pump_prompt checks this flag).
        self.system_queue_consumer: bool = False
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
        # Highest DB message id before this pump's first row — the id-based
        # cutoff that excludes in-flight turn saves from chat_history on live
        # reconnect (id-based, not a row count: a *windowed* resume can't index
        # a 50-row page — see ws/dashboard.py truncation). None until the start
        # job — a chat-lane read, so it is ordered after the previous pump's
        # rows — lands: no row of this turn can exist before that, so None reads
        # as "withhold nothing". Each save advances it as it lands.
        self._db_msg_cutoff_id: int | None = None
        # LLM chat-title generation (services/title_generator.py): the one-time
        # title upgrade for chats not yet LLM-titled. Fired in the TEXT handler
        # once the first response crosses _TITLE_CHAR_THRESHOLD, in the
        # TOOL_USE handler at _TITLE_TOOL_THRESHOLD tool calls, or after the
        # turn-end save for a short first response. Task chats title like every
        # chat (they live in the sidebar's Task history view); only a meeting
        # pump is excluded (its chat is the PARENT chat, titled by its own
        # turn). Armed by default — the service's atomic claim makes a
        # redundant attempt a no-op — and disarmed by the start job when the
        # row says the title was already generated.
        self._title_armed = source_type != session_kind.MEETING.source_type
        self._completed = False  # PRODUCER_DONE reached: all_done goes out after the drain
        self._owner_sub = ""
        self._tool_seen = 0  # tool calls this pump — the title tool-count trigger
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

        # Stop-and-send flags ({"fired": bool, "note": bool}) — assigned ONLY
        # by dashboard_chat._start_new_stream, whose producer drains
        # message_queue (the same ownership pattern as the shared queue
        # lists). None = foreign producer (task/meeting/duplex/delegate-echo/
        # recovery pumps) that never drains typed messages: the fire site
        # treats None as "queue-only", so an interrupt can never destroy a
        # turn whose queued message nothing would deliver.
        self.stop_and_send: dict | None = None

    # Tools that have dedicated events (task_spawn, plan_mode, the delegate
    # pill) — skip tool persistence: by ROLE, plus the platform's own
    # delegate MCP tool (the dashboard's skip set names the same constant).
    _SKIP_TOOL_PERSIST_ROLES = frozenset({tool_roles.SUBAGENT, tool_roles.PLAN_ENTER, tool_roles.PLAN_EXIT})

    @classmethod
    def _skips_tool_persist(cls, name: str) -> bool:
        return tool_roles.role_of(name) in cls._SKIP_TOOL_PERSIST_ROLES or name == DELEGATE_TOOL

    @property
    def is_done(self) -> bool:
        return self._done

    def attach(self, *, bounded: bool = False) -> asyncio.Queue:
        """Attach a WS consumer. Returns queue to read pump events from.

        Fan-out: every attached consumer gets every frame. A second viewer
        (another device/tab of the same user, or a teammate on a shared chat)
        must never displace the first — the old single-slot steal made the
        viewers' 3s `_task_pump_poll` fight over the stream, wholesale-reloading
        the chat on every takeover (the "chat refreshes mid-generation" bug).

        The pending delta goes to the existing viewers BEFORE the new queue
        joins: the newcomer's live_state snapshot already holds its text.
        ``bounded`` (dashboard viewers) caps the queue (``_ViewerQueue``). A
        pump that already ended answers ``pump_ended`` at once.
        """
        self._flush_deltas()
        q = _ViewerQueue(bounded)
        if self._ended:
            q.put_nowait({"pump_type": wire.PUMP_ENDED})
        self._ws_queues.append(q)
        return q

    def detach(self, q: asyncio.Queue):
        """Detach ONE WS consumer's queue. Pump continues independently.

        ``q`` is the queue the caller received from :meth:`attach` — removing
        it never touches other viewers' queues, so a stale detach (an old
        socket's close path racing a new viewer) is inherently harmless.
        """
        with contextlib.suppress(ValueError):
            self._ws_queues.remove(q)

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

    def clear_permission_state(self):
        """Drop the active + buffered permission slots without advancing.

        Stop-and-send companion: the graceful interrupt's auto-deny releases
        the hook waiters (``resolve_session_permissions``) but nothing would
        clear the PUMP's slot — the drain turn's next permission would then
        buffer invisibly behind a dead card and hang the turn. (The Stop
        button never hits this: no drain follows it.) Buffered prompts are
        dropped, not shown — their waiters were just denied with the turn.
        """
        self._permission_active = None
        self._permission_buffer.clear()
        _pending_permissions.pop(self.session_id, None)
        live = _chat_streaming_state.get(self.chat_id)
        if live:
            live["pending_permission"] = None

    async def _show_permission(self, perm_data: dict):
        """Set a permission as active and forward to WS."""
        self._permission_active = perm_data
        _pending_permissions[self.session_id] = perm_data
        live = _chat_streaming_state.get(self.chat_id)
        if live:
            live["pending_permission"] = perm_data

        evt_type = perm_data.get("event_type", "")
        if evt_type == wire.ITEM_PLAN_REVIEW:
            filename = perm_data.get("filename", "")
            await self._forward(
                {"pump_type": wire.PUMP_PLAN_REVIEW, "perm_data": perm_data,
                 "filename": filename},
            )
        elif evt_type == wire.ITEM_QUESTION_PROMPT:
            # Codex request_user_input: the daemon HOLDS the turn open on this
            # question, so the turn-end ping never fires for it. Surface the card
            # and fire a "needs your input" ephemeral NOW (away/FCM aware) so a
            # user who stepped away still gets pinged. Do NOT touch chat_status —
            # the turn is still streaming (no "ready" flip mid-held-turn).
            await self._forward(
                {"pump_type": wire.PUMP_QUESTION_PROMPT, "perm_data": perm_data},
            )
            _q_cid = self.chat_id

            def _row_meeting_label() -> tuple[dict | None, bool, str]:
                row = task_store.get_chat(_q_cid)
                if not row:
                    return None, False, ""
                return (
                    row,
                    bool(task_store.get_active_meeting_for_chat(_q_cid)),
                    notification_manager.agent_label(row["agent"]) if row.get("agent") else "",
                )

            chat, _in_meeting, _label = await run_db(_row_meeting_label)
            if chat and self.source_type != session_kind.TASK.source_type and not _in_meeting:
                qs = (perm_data.get("tool_input") or {}).get("questions") or []
                first_q = (qs[0].get("question") if qs and isinstance(qs[0], dict)
                           else "") or "Waiting for your answer"
                asyncio.create_task(notification_manager.fire_ephemeral(
                    chat["user_sub"],
                    title=f"{_label or chat.get('agent') or ''} needs your input",
                    body=first_q,
                    chat_id=self.chat_id,
                ))
        else:
            forward_data = {"pump_type": wire.PUMP_PERMISSION_PROMPT, "perm_data": perm_data}
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

    # Cap the pending-message backlog per pump. A client that holds a turn
    # open and keeps sending `chat` frames (which, when steer isn't available,
    # queue with no DB write) would otherwise grow this list without bound in
    # the shared proxy process. ~64 unsent messages is far beyond any real
    # between-turns backlog.
    _QUEUE_CAP = 64

    def queue_message(self, item: TurnInput) -> int:
        """Queue a user message for the producer. Returns the index, or -1 when
        the backlog cap is hit (caller surfaces a 'queue full' notice)."""
        if len(self.message_queue) >= self._QUEUE_CAP:
            return -1
        self.message_queue.append(item)
        return len(self.message_queue) - 1

    def cancel_queued(self, index: int) -> TurnInput | None:
        """Remove queued message by index. Returns the removed item or None."""
        if 0 <= index < len(self.message_queue):
            return self.message_queue.pop(index)
        return None

    def cancel_all_queued(self) -> str:
        """Remove all queued messages (artifact interactions too — they were
        never delivered, so nothing persists). Returns combined user text."""
        combined = "\n\n".join(i.text for i in self.message_queue) if self.message_queue else ""
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
            self._turn_blocks.append(self._stamp_speaker({"type": wire.TEXT, "content": text}))
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

    def _buffer_preview(self, evt: dict, identity: str, live: dict | None) -> None:
        """A deferred artifact (the document preview): replace the push of
        the same identity in place in the turn and live lists — dropping the
        replaced push's snapshot, which the dashboard never saw because
        previews forward only at the flush — else append; then hold the
        latest per identity for ``_flush_pending_previews``."""
        key = evt[identity]
        replaced = False
        for i, block in enumerate(self._turn_blocks):
            if block.get("type") == evt["type"] and block.get(identity) == key:
                old_snap = block.get("snapshot_id") or ""
                if old_snap and old_snap != evt.get("snapshot_id"):
                    from services.media import preview_snapshots
                    chat_writer.submit(
                        self.chat_id,
                        functools.partial(preview_snapshots.delete_snapshot, self.chat_id, old_snap),
                        label="snapshot_delete",
                    )
                self._turn_blocks[i] = evt
                replaced = True
                break
        if not replaced:
            self._turn_blocks.append(evt)
        self._pending_previews[key] = evt
        if live:
            for i, lb in enumerate(live["live_blocks"]):
                if lb.get("type") == evt["type"] and lb.get(identity) == key:
                    live["live_blocks"][i] = evt
                    break
            else:
                live["live_blocks"].append(evt)

    async def _flush_pending_previews(self):
        """Forward buffered document previews to dashboard (called at turn end)."""
        if self._pending_previews:
            self._gc_snapshots_pending = True
        for evt in self._pending_previews.values():
            await self._forward({"pump_type": wire.PUMP_WS_EVENT, "event": evt})
        self._pending_previews.clear()

    def _save_turn_blocks(self) -> asyncio.Future | None:
        """Persist the turn blocks accumulated so far, in order, off the loop.

        The rows are serialised HERE (the block dicts keep mutating on the
        loop afterwards — plan_review actions, live badges), the accumulator
        is cleared, and ONE writer job per call goes on the chat's lane:
        inserts → snapshot GC → the last message id. When the job lands the
        id-based cutoff advances and exactly the live blocks it persisted are
        trimmed from live_state, so a viewer reconnecting during the write
        still sees the turn (live_state), and afterwards from chat_history —
        never neither, never both. Returns the job's future, or None when
        nothing was queued.
        """
        if self.chat_id in _recovery_suppress_flush:
            # This turn will be re-adopted + re-persisted from the satellite
            # after the restart — a shutdown flush here would duplicate it.
            self._turn_blocks.clear()
            return None
        if not self._turn_blocks:
            return None
        rows = _serialize_turn_rows(self._turn_blocks)
        self._turn_blocks.clear()
        self._todo_block = None
        gc_pending, self._gc_snapshots_pending = self._gc_snapshots_pending, False
        chat_id = self.chat_id
        live = _chat_streaming_state.get(chat_id)
        live_list = live.get("live_blocks") if live else None
        live_len = len(live_list) if live_list is not None else 0

        def _job() -> int:
            # One transaction and ONE search-row rebuild for the turn's rows.
            task_store.add_chat_messages_batch(chat_id, [
                ("assistant", content, "", event_data) if role == "assistant"
                else ("event", "", event_type, event_data)
                for role, content, event_type, event_data in rows
            ])
            if gc_pending:
                # The flushed preview rows are persisted now — prune this
                # chat's unreferenced version-pinned snapshots (dismissed
                # history etc.).
                try:
                    from services.media import preview_snapshots
                    preview_snapshots.gc_chat(chat_id)
                except Exception:
                    logger.debug("preview snapshot GC failed", exc_info=True)
            return task_store.get_last_chat_message_id(chat_id)

        fut = chat_writer.submit(chat_id, _job, label="turn_blocks")

        def _landed(f: asyncio.Future) -> None:
            if f.cancelled() or f.exception() is not None:
                return
            # Advance the cutoff so the saved content (completed meeting
            # turns included) comes from chat_history on reconnect, not
            # truncated as in-flight.
            self._db_msg_cutoff_id = max(self._db_msg_cutoff_id or 0, f.result() or 0)
            # Trim exactly the blocks this job persisted; blocks appended
            # since (the next speaker) stay live. A superseding pump replaces
            # the live dict — then there is nothing of ours to trim.
            cur = _chat_streaming_state.get(chat_id)
            if (cur is not None and live_list is not None
                    and cur.get("live_blocks") is live_list):
                del live_list[:live_len]

        fut.add_done_callback(_landed)
        return fut

    def _submit_chat_update(self, label: str, **fields) -> None:
        """A chat-row write that must land in order with this turn's rows but
        that nothing waits for (goal, compaction gauge, modes, thread id)."""
        chat_id = self.chat_id
        chat_writer.submit(
            chat_id, functools.partial(task_store.update_chat, chat_id, **fields),
            label=label,
        )

    def _seed_cost_baseline(self, row: dict) -> None:
        """The engine's saved running cost total for THIS session, off the
        chat row (`engine_cost_total` / `engine_cost_session_id`, written at
        every cumulative-cost turn): Claude Code ≥ 2.1.277 resumes a session
        from its saved total, and the in-memory baseline
        (`_session_cumulative_cost`) dies with the proxy process. Seeded only
        when the map still has no entry (a late callback on a stalled lane
        never overwrites what the METADATA branch already recorded) and only
        for the SAME session id (a fresh session on this chat — an engine
        switch, a rebuilt history — starts at 0, as ever)."""
        if self.session_id in _session_cumulative_cost:
            return
        if (row.get("engine_cost_session_id") or "") != self.session_id:
            return
        seeded = float(row.get("engine_cost_total") or 0.0)
        if seeded > 0:
            self._last_session_cost = seeded
            _session_cumulative_cost[self.session_id] = seeded

    def _submit_turn_start(self) -> None:
        """Turn-start persistence, never awaited: the cutoff read + the row
        that says whether the title was already generated (one lane job —
        ordered after the previous pump's rows and before any of ours), plus
        the resumed-task History open. The "streaming" sidebar dot fires
        synchronously when the caller supplied the owner, else from the job.
        A stalled lane must not stop this turn from streaming: the cutoff
        stays None (withhold nothing) until the job lands."""
        chat_id = self.chat_id
        if self._chat_owner:
            self._owner_sub = self._chat_owner
            notification_manager.broadcast_chat_status(
                self._owner_sub, chat_id, chat_phase.STREAMING, agent=self._chat_agent,
            )

        def _job() -> tuple[int, dict]:
            row = task_store.get_chat(chat_id) or {}
            return task_store.get_last_chat_message_id(chat_id), row

        def _started(f: asyncio.Future) -> None:
            if f.cancelled() or f.exception() is not None:
                return
            cutoff, row = f.result()
            self._db_msg_cutoff_id = max(self._db_msg_cutoff_id or 0, cutoff or 0)
            if row.get("title_generated"):
                self._title_armed = False
            self._seed_cost_baseline(row)
            if not self._chat_owner:
                # Light this chat's sidebar dot on every device of its owner
                # — the authoritative turn-start signal, viewed or background.
                # Shared-only chats (synthetic agent:: owner) fan out to every
                # user of the agent via the agent arg.
                self._owner_sub = row.get("user_sub") or ""
                if self._owner_sub:
                    notification_manager.broadcast_chat_status(
                        self._owner_sub, chat_id, chat_phase.STREAMING,
                        agent=row.get("agent") or "",
                    )

        chat_writer.submit(chat_id, _job, label="turn_start").add_done_callback(_started)
        if session_kind.is_task_chat_id(chat_id) and self.source_type != session_kind.TASK.source_type:
            # Dashboard-RESUMED task conversation (the scheduler's own runs are
            # source_type == "task"): reflect the live turn in the Task History
            # row, which otherwise freezes on its pre-resume terminal state.
            # Terminal-states-only guard: never touch a run some other pump or
            # the scheduler still owns. The matching close is the turn-end job.
            chat_writer.submit(
                chat_id,
                functools.partial(
                    task_store.update_latest_run_status_for_chat, chat_id, run_status.RUNNING,
                    only_from=tuple(sorted(run_status.TERMINAL)),
                ),
                label="run_open",
            )

    def _submit_cost_job(self) -> asyncio.Future | None:
        """Persist cost + context + the usage rows, once per pump, as ONE lane
        job: the total_cost read-modify-write and the usage rows are atomic
        against any other writer of this chat. Fire on ANY token activity,
        not only cost>0, so $0 turns (local / unpriced providers) still
        persist token counts + message_count (analytics needs them). The
        subscription attribution is snapshotted on the loop now — an engine
        switch after the turn must not re-attribute it."""
        has_activity = (self._total_cost_delta > 0
                        or self._input_tokens or self._output_tokens)
        if self.chat_id in _recovery_suppress_flush:
            has_activity = False  # replay re-persists; don't double
        if not has_activity or self._cost_saved:
            return None
        self._cost_saved = True
        snap = self._usage_snapshot()
        chat_id = self.chat_id

        def _job() -> None:
            chat_row = task_store.get_chat(chat_id)
            if not chat_row:
                return
            old_cost = chat_row.get("total_cost") or 0
            updates = {"total_cost": old_cost + snap["total_cost_delta"]}
            if snap["context_used"] > 0:
                updates["context_used"] = snap["context_used"]
                updates["context_max"] = snap["context_max"]
            if snap["cache_write"] > 0:
                updates["cache_read"] = snap["cache_read"]
                updates["cache_write"] = snap["cache_write"]
                updates["output_tokens"] = snap["output_tokens"]
            task_store.update_chat(chat_id, **updates)
            _record_usage(chat_row, snap)

        return chat_writer.submit(chat_id, _job, label="turn_cost")

    def _usage_snapshot(self) -> dict:
        """Plain values for the cost job. The subscription that served this
        session is read NOW: the pool releases it on close/switch, which can
        happen before the job runs."""
        from services.engines import subscription_pool
        snap = {
            "session_id": self.session_id,
            "chat_id": self.chat_id,
            "scope": self.scope,
            "source_type": self.source_type,
            "total_cost_delta": self._total_cost_delta,
            "llm_cost_delta": self._llm_cost_delta,
            "input_tokens": self._input_tokens,
            "output_tokens": self._output_tokens,
            "cache_read": self._cache_read,
            "cache_write": self._cache_write,
            "context_used": self._context_used,
            "context_max": self._context_max,
            "mcp_cost_by_key": dict(self._mcp_cost_by_key),
            "sub_id": "default",
            "payer": None,
        }
        try:
            # Attribute the LLM turn to the subscription that served it, so the
            # pool can route new chats to the least-consumed account.
            snap["sub_id"] = subscription_pool.get_session_subscription(self.session_id) or "default"
            if self.scope == "agent":
                snap["payer"] = subscription_pool.get_session_payer_sub(self.session_id)
        except Exception:
            logger.debug("usage snapshot: subscription lookup failed", exc_info=True)
        return snap

    async def _cost_billed_for(self, session_id: str) -> bool:
        """``cost_billed`` for a metadata event: does this session's credential
        cost real money (API key / relay → shown) or is it a subscription /
        local-model activity estimate (→ hidden)? Resolved per event, on the
        DB executor (never the loop): one pump can span queued turns and a
        direct-llm provider switch re-binds the session between them, so a
        per-pump cache would stamp the previous kind. A failure answers True
        (show — today's behaviour)."""
        from services.engines import subscription_pool
        try:
            return await run_db(subscription_pool.session_cost_billed, session_id)
        except Exception:
            logger.debug("cost_billed lookup failed", exc_info=True)
            return True

    def _submit_turn_end_job(self) -> asyncio.Future:
        """The turn's last lane job: ``last_response_at`` (after every message
        row — the sidebar unread indicator), the resumed-task History close
        (compare-and-set: only a turn this pump opened), and the chat row +
        meeting flag + agent label the loop-side broadcasts consume — no
        re-read on the loop."""
        chat_id = self.chat_id
        now_iso = datetime.now(timezone.utc).isoformat()
        resumed_task = (session_kind.is_task_chat_id(chat_id)
                        and self.source_type != session_kind.TASK.source_type)
        aborted = self._abort_requested

        def _job() -> tuple[dict | None, bool, str]:
            with contextlib.suppress(Exception):
                task_store.update_chat(chat_id, last_response_at=now_iso)
            if resumed_task:
                # The user stopping the turn reads as cancelled, everything
                # else as completed. only_from=("running",): close ONLY a turn
                # this pump opened — a wedged-pump reap stamps failed + reason
                # before aborting us, and that verdict must survive.
                try:
                    task_store.update_latest_run_status_for_chat(
                        chat_id, run_status.CANCELLED if aborted else run_status.COMPLETED,
                        only_from=(run_status.RUNNING,),
                    )
                except Exception as e:
                    logger.debug("resumed-task run status close failed: %s", e)
            chat = task_store.get_chat(chat_id)
            if not chat:
                return None, False, ""
            meeting = bool(task_store.get_active_meeting_for_chat(chat_id))
            label = notification_manager.agent_label(chat["agent"]) if chat.get("agent") else ""
            return chat, meeting, label

        return chat_writer.submit(chat_id, _job, label="turn_end")

    async def _drain_writes(self) -> None:
        """Await this chat's writer lane. Under a slow database the pump
        coroutine parks here while the loop keeps serving everyone else; a
        WARNING every 10 s names the wait (the loop watchdog cannot see a
        wait that leaves the loop free)."""
        waited = 0.0
        while not await chat_writer.drain(self.chat_id, timeout=10.0):
            if chat_writer.pending(self.chat_id) == 0:
                break  # the lane was cancelled — nothing more will land
            waited += 10.0
            logger.warning(
                f"ChatStreamPump chat={self.chat_id}: turn rows still landing "
                f"after {waited:.0f}s — database slow?"
            )

    def _deliver(self, item: dict) -> None:
        """Put ``item`` on every subscriber's queue; never waits on a viewer.
        A bounded queue at its cap is cleared and gets one PUMP_RESYNC."""
        for q in list(self._ws_queues):
            if q.overflowed:
                continue
            if q.bounded and q.qsize() >= _VIEWER_QUEUE_MAX:
                while not q.empty():
                    q.get_nowait()
                q.put_nowait({"pump_type": PUMP_RESYNC})
                q.overflowed = True
                logger.warning(
                    f"ChatStreamPump chat={self.chat_id}: a viewer fell "
                    f"{_VIEWER_QUEUE_MAX} frames behind; resyncing it")
                continue
            q.put_nowait(item)

    def _flush_deltas(self) -> None:
        """Deliver the pending delta frame, if any (before any other frame)."""
        if self._delta_timer is not None:
            self._delta_timer.cancel()
            self._delta_timer = None
        item, self._delta_item, self._delta_key = self._delta_item, None, None
        if item is not None:
            self._delta_sent_at = time.monotonic()
            self._deliver(item)

    async def _forward(self, item: dict):
        """Forward event to every attached WS subscriber."""
        self._flush_deltas()
        self._deliver(item)

    async def _forward_event(self, event_type: str, payload: dict):
        """Forward a CommonEvent as the frame the catalogue names for it
        (``wire.WIRE_OF_EVENT``), the payload spread in."""
        await self._forward({"pump_type": wire.PUMP_WS_EVENT,
                             "event": {"type": wire.WIRE_OF_EVENT[event_type], **payload}})

    async def _forward_delta(self, event_type: str, payload: dict):
        """``_forward_event`` for a text or thinking delta, coalesced: at once
        when no delta frame went out in the last ``_DELTA_FLUSH_S``, else
        merged into one pending frame flushed at the window's end."""
        key = _delta_key(event_type, payload)
        if key is None:
            await self._forward_event(event_type, payload)
            return
        if not self._ws_queues:
            return
        event = {"type": wire.WIRE_OF_EVENT[event_type], **payload}
        pending = self._delta_item
        if pending is not None and self._delta_key == key:
            merged = pending["event"]
            if key[0] == TEXT:
                merged["content"] += event["content"]
            elif key[0] == THINKING:
                merged["text"] += event["text"]
            else:
                merged["estimated_tokens"] = event["estimated_tokens"]
            return
        self._flush_deltas()
        item = {"pump_type": wire.PUMP_WS_EVENT, "event": event}
        now = time.monotonic()
        if now - self._delta_sent_at >= _DELTA_FLUSH_S:
            self._delta_sent_at = now
            self._deliver(item)
            return
        self._delta_item, self._delta_key = item, key
        self._delta_timer = asyncio.get_running_loop().call_later(
            self._delta_sent_at + _DELTA_FLUSH_S - now, self._flush_deltas)

    def push_ws_event(self, event: dict) -> bool:
        """External push (scheduler / session_state): fan a ready-made ws_event
        to every attached subscriber. Returns whether anyone received it."""
        if not self._ws_queues:
            return False
        self._flush_deltas()
        self._deliver({"pump_type": wire.PUMP_WS_EVENT, "event": event})
        return True

    def start(self) -> asyncio.Task:
        """Start the pump's background processing task. The live state
        exists from here on, so a viewer attaching before the task first
        runs still gets its live_state snapshot."""
        self._init_live_state()
        self._task = asyncio.create_task(self._run())
        return self._task

    def _init_live_state(self) -> None:
        self._live_ready = True
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

    async def _run(self):
        """Main pump loop: reads CommonEvent objects, updates state, saves to DB."""
        if not self._live_ready:
            self._init_live_state()

        try:
            # Inside the try: whatever the start job does, the finally below
            # must run — a pump that never leaves _active_pumps is the
            # stuck-chat failure class. The matching "ready" fires in the
            # finally when the turn genuinely ends.
            self._submit_turn_start()
            while True:
                # Poll permission queue (non-blocking)
                if self.perm_queue is not None:
                    try:
                        perm_data = self.perm_queue.get_nowait()
                        await self._handle_perm_event(perm_data)
                    except asyncio.QueueEmpty:
                        pass

                # Read CommonEvent from event queue (faster poll when WS attached)
                timeout = 0.15 if self._ws_queues else 2.0
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
                    with contextlib.suppress(Exception):
                        await run_db(task_store.touch_chat, self.chat_id)

                if event.type == ERROR:
                    err_msg = event.data.get("message", "")
                    self.last_error = err_msg or "engine error"
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
                    await self._forward({"pump_type": wire.PUMP_ERROR, "message": err_msg})
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
                        self._turn_blocks.append({"type": wire.TEXT, "content": err_msg})
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
                    save_fut = self._save_turn_blocks()
                    self._submit_cost_job()
                    # LLM chat-title: a short first response never crossed the
                    # streaming threshold — fire once the turn-end save has
                    # landed (the service reads the first prompt + response
                    # from the DB). The atomic claim dedupes against the
                    # threshold fire. One pump == one turn.
                    if self._title_armed:
                        self._title_armed = False
                        from services import title_generator
                        _title_cid = self.chat_id

                        def _fire_title(_f=None) -> None:
                            asyncio.create_task(
                                title_generator.request_chat_title(_title_cid)
                            )

                        if save_fut is not None:
                            save_fut.add_done_callback(_fire_title)
                        else:
                            _fire_title()
                    # all_done goes out from the finally, after this turn's rows
                    # have landed: "done" means "persisted" to the queue drain
                    # and the resume re-send.
                    self._completed = True
                    break

                if event.type == QUEUE_TURN:
                    # During a meeting turn, flush+save accumulated blocks first
                    # so the user message appears after prior content in DB order.
                    if self._meeting_agent:
                        self._flush_pending_text()
                        if self._turn_blocks:
                            self._save_turn_blocks()
                    meeting_agent = event.data.get("meeting_agent")
                    # The row's meta: the batch's attachments (photos and
                    # files the drained messages carried) plus, in a
                    # meeting, the moderator's identity badge.
                    event_meta: dict = dict(event.data.get("event_data") or {})
                    if meeting_agent:
                        from storage.agents import agent_store as _agent_store
                        ad = _agent_store.get_agent(meeting_agent)
                        event_meta.update({
                            "agent_slug": meeting_agent,
                            "agent_display_name": (ad or {}).get("display_name", meeting_agent),
                            "agent_color": (ad or {}).get("color", ""),
                            "badge": "meeting prompt",
                        })
                    event_data_str = json.dumps(event_meta) if event_meta else ""
                    # Lane-ordered after the blocks saved above; the frame is
                    # the live echo, not a read.
                    chat_writer.submit(
                        self.chat_id,
                        functools.partial(
                            task_store.add_chat_message, self.chat_id, "user",
                            event.data["text"], event_data=event_data_str,
                        ),
                        label="queue_turn",
                    )
                    forwarded = {"pump_type": wire.PUMP_QUEUE_TURN, "text": event.data["text"]}
                    if event_meta.get("images"):
                        forwarded["images"] = event_meta["images"]
                    if event_meta.get("files"):
                        forwarded["files"] = event_meta["files"]
                    await self._forward(forwarded)
                    continue

                if event.type == ARTIFACT_TURN:
                    # Drained backchannel interactions (artifact sends AND
                    # app send_prompt actions, kind-tagged): one distinct
                    # event row per entry (NEVER a "user" row — the row type +
                    # the framed prompt carry the provenance), forwarded live
                    # so the sender's transcript shows the chip at delivery.
                    from ws import artifact_interactions as _ai
                    _art_rows = [
                        (_ai.event_type(it), _ai.event_row_json(it))
                        for it in event.data.get("interactions", [])
                    ]
                    _art_cid = self.chat_id

                    def _art_job(_rows=_art_rows) -> None:
                        for _etype, _edata in _rows:
                            task_store.add_chat_message(
                                _art_cid, "event", "", event_type=_etype, event_data=_edata,
                            )

                    if _art_rows:
                        chat_writer.submit(_art_cid, _art_job, label="artifact_turn")
                    for it in event.data.get("interactions", []):
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
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self.producer

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
                    "type": wire.THINKING, "content": self._thinking_text,
                }))
                self._thinking_text = ""
            for tool_evt in self._active_tools.values():
                self._turn_blocks.append(tool_evt)
            self._save_turn_blocks()
            self._submit_cost_job()

            if self.implementing_plan:
                chat_writer.submit(
                    self.chat_id,
                    functools.partial(
                        task_store.update_chat_plan_status,
                        self.chat_id, self.implementing_plan, "implemented",
                    ),
                    label="plan_status",
                )

            # Supersession guard: a NEWER pump may already own this chat — an
            # abort + fast resend starts the next turn's pump while this one is
            # still unwinding. Only the still-current pump may stamp the turn
            # end, tear down shared per-chat state or declare the chat ready
            # below; a late pop / "ready" broadcast from the aborted pump would
            # wipe the new turn's live state and flip the live UI idle mid-turn
            # (the session_id check alone doesn't cover Codex, where abort
            # keeps the daemon and the next turn reuses the same session_id).
            was_active_pump = _active_pumps.get(self.chat_id) is self
            end_fut = self._submit_turn_end_job() if was_active_pump else None
            end_row: dict | None = None
            end_meeting = False
            end_label = ""
            still_active = was_active_pump
            try:
                # Every row of this turn is committed before the pump leaves
                # _active_pumps, forwards all_done and declares the chat ready
                # — "done" means "persisted" to the post-turn queue drain and
                # the resume re-send. The loop stays free meanwhile.
                await self._drain_writes()
                if end_fut is not None and not end_fut.cancelled() and end_fut.exception() is None:
                    end_row, end_meeting, end_label = end_fut.result()
                if self._completed:
                    await self._forward({"pump_type": wire.PUMP_ALL_DONE})
            finally:
                # Shared-state teardown runs whatever happened above (a
                # cancellation inside the drain must not leave the pump
                # registered as live). Re-check ownership: a newer pump may
                # have registered during the drain.
                # Clear live state + deregister (only if we're still the
                # current pump). EXCEPTION: while background subagents are
                # still running, keep a reduced live-state residual — the
                # running-agent badges only — so a reconnect between this
                # turn's end and the nudge turn still renders them. The
                # turn's text/widgets are in the DB now (drained above), so
                # live_blocks are dropped to avoid double-rendering against
                # chat history; the status bar's active_agents is the only
                # thing the DB can't reconstruct mid-flight. Each agent's
                # badge clears via mark_subagent_done as it finishes; the
                # nudge turn's pump pops the state fully when has_pending is
                # finally False.
                still_active = _active_pumps.get(self.chat_id) is self
                live = _chat_streaming_state.get(self.chat_id)
                if still_active and live and live.get("session_id") == self.session_id:
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
                if still_active:
                    del _active_pumps[self.chat_id]

                # Signal any remaining subscribers (a later attach gets it too)
                self._flush_deltas()
                self._ended = True
                self._deliver({"pump_type": wire.PUMP_ENDED})

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
                if not still_active:
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
                    self._fire_end_of_turn(end_row, end_meeting, end_label)
            except Exception:
                pass  # Don't break pump cleanup for notification failure

            # Shared-library turn-end reconcile: a sandbox delete/write inside
            # a library (mirror or source) passes no proxy chokepoint — kick
            # the projector now so the agent's own change lands before it
            # reports, instead of waiting for the 5-minute sweep. No-op for
            # agents without library rows (one indexed SELECT). Uses the row
            # the turn-end job returned — no loop-side read.
            try:
                if end_row and end_row.get("agent"):
                    from services.knowledge import library_projector
                    library_projector.schedule_reconcile_for_agent(end_row["agent"])
            except Exception:
                pass

            logger.info(
                f"ChatStreamPump ended: chat={self.chat_id}, "
                f"blocks={len(self._turn_blocks)}, plan={self.implementing_plan or 'none'}"
            )

    def _fire_end_of_turn(self, chat: dict | None, in_meeting: bool,
                          agent_label: str) -> None:
        """The turn genuinely ended: clear the sidebar dot on every device (a
        background chat has no other live signal), then fire the origin-routed
        end-of-turn alert. ``chat`` / ``in_meeting`` / ``agent_label`` come from
        the turn-end job (read after the rows landed). The alert is skipped
        during meetings (per-speaker turn ends are not completions) and for
        a pump the scheduler drives (``source_type == session_kind.TASK.source_type``
        — the DRIVER's kind, a captured wake into its warm session included) —
        a task's completion alert is its ``notification_mode`` contract, and an
        extra "finished" push on top is noise. A continued (re-warmed) task chat
        runs through a dashboard-driven pump (``DASHBOARD.source_type``) and keeps the normal
        per-turn signal — it is the only completion signal those follow-up
        turns have."""
        if not chat:
            return
        notification_manager.broadcast_chat_status(
            chat["user_sub"], self.chat_id, chat_phase.READY,
            agent=chat.get("agent") or "",
        )
        if self.source_type == session_kind.TASK.source_type:
            return
        if not in_meeting:
            asyncio.create_task(notification_manager.fire_ephemeral(
                chat["user_sub"],
                title=f"{agent_label or chat.get('agent') or ''} finished",
                body="Response ready",
                chat_id=self.chat_id,
            ))

    @staticmethod
    def _live_append_text(live: dict, text: str):
        """Append text to the last text block in live_blocks, or create a new one."""
        blocks = live["live_blocks"]
        if blocks and blocks[-1].get("type") == wire.LIVE_TEXT:
            blocks[-1]["content"] += text
        else:
            blocks.append({"type": wire.TEXT, "content": text})

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
            await self._forward_event(THINKING, {"phase": "end", "text": ""})

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
                await self._forward_delta(TEXT, {"content": content})
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
            await self._forward_delta(THINKING, ed)
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
                        {"type": wire.THINKING, "content": self._thinking_text})
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
            await self._forward_event(TOOL_USE, ed)
            tool_name = ed.get("name", "")
            # LLM chat-title: tool-count early trigger. Counted at branch top —
            # before _skips_tool_persist — so subagent/delegate spawns count.
            self._tool_seen += 1
            if self._title_armed and self._tool_seen >= _TITLE_TOOL_THRESHOLD:
                self._title_armed = False
                from services import title_generator
                asyncio.create_task(title_generator.request_chat_title(
                    self.chat_id, assistant_excerpt="".join(self._pending_text),
                ))
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
            if not self._skips_tool_persist(tool_name):
                tool_id = ed.get("tool_id") or tool_name
                self._flush_pending_text()
                tool_block = self._stamp_speaker({
                    "type": wire.PERSISTED_TOOL, "name": tool_name,
                    "tool_id": tool_id, "summary": "", "active": True,
                    "tool_input": None,
                    "_insert_idx": len(self._turn_blocks),  # track position for correct DB ordering
                })
                self._active_tools[tool_id] = tool_block
                if live:
                    live["active_tools"] = list(self._active_tools.values())
                    live["live_blocks"].append(tool_block)

        elif event.type == TOOL_INPUT:
            await self._forward_event(TOOL_INPUT, ed)
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
            if tool_roles.role_of(name) == tool_roles.WRITE:
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
                await self._forward({"pump_type": wire.PUMP_WS_EVENT,
                    "event": {"type": wire.TOOL_RESULT,
                               "tool_name": tool_name,
                               "tool_use_id": tool_id,
                               "summary": summary,
                               "result_content": result_content}})
            await self._forward_event(TOOL_RESULT, ed)
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
                            await self._forward({"pump_type": wire.PUMP_WS_EVENT, "event": {
                                "type": wire.MCP_COST,
                                "cost_usd": hit.amount,
                                # A tool fee is money whatever credential the
                                # LLM runs on — the gauge shows it even on a
                                # subscription / local-model chat.
                                "cost_billed": True,
                                "provider": hit.provider,
                                "model": hit.model,
                                "tool": plain_tool,
                                "mcp": mcp_name,
                            }})
                except Exception:
                    logger.exception("mcp cost evaluation failed for %s", tool_name)

        elif event.type == SUBAGENT_START:
            await self._forward_event(SUBAGENT_START, ed)
            self._flush_pending_text()
            self._turn_blocks.append({"type": wire.TASK_SPAWN, **ed})
            is_bg = ed.get("run_in_background", False)
            if live:
                agent_block = {
                    "type": wire.LIVE_AGENT,
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
            await self._forward_event(SUBAGENT_END, {"tool_use_id": tuid})
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
            await self._forward_event(BG_COMMAND_START, ed)
            self._flush_pending_text()
            self._turn_blocks.append({"type": wire.BG_COMMAND_SPAWN, **ed})
            if live:
                command_block = {
                    "type": wire.LIVE_COMMAND,
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
            await self._forward_event(BG_COMMAND_END, {"tool_use_id": tuid,
                                                       "status": ed.get("status", "")})
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
            await self._forward_event(WORKFLOW_START, {
                "tool_use_id": tuid, "workflow_name": ed.get("workflow_name", ""),
            })
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
                await self._forward_event(WORKFLOW_PROGRESS, {
                    "tool_use_id": tuid, "workflow_progress": progress,
                })

        elif event.type == WORKFLOW_END:
            tuid = ed.get("tool_use_id", "")
            self._wf_last_forward.pop(tuid, None)
            await self._forward_event(WORKFLOW_END, {"tool_use_id": tuid})
            if live and tuid in live.get("workflows", {}):
                live["workflows"][tuid]["active"] = False

        elif event.type == DELEGATE_SPAWN:
            evt = {"type": wire.DELEGATE_SPAWN, **ed}
            await self._forward({"pump_type": wire.PUMP_WS_EVENT, "event": evt})
            self._flush_pending_text()
            self._turn_blocks.append(evt)
            if live:
                delegate_block = {
                    "type": wire.LIVE_DELEGATE,
                    "task_id": ed.get("task_id", ""),
                    "task_name": ed.get("task_name", ""),
                    "agent": ed.get("agent", ""),
                    # Full prompt for the expandable pill (reconnect parity).
                    "prompt": ed.get("prompt", ""),
                    "active": True,
                    "status": run_status.RUNNING,  # a DELEGATE_RESULTS word on terminal
                }
                live["active_delegates"].append(delegate_block)
                live["live_blocks"].append(delegate_block)

        elif event.type == CHECK_VERDICT:
            # A check's verdict (CHECKS.md "Rendering"): the card rides the
            # turn in order — forwarded live, kept in the live state for a
            # reconnect, persisted as an event row with the turn's blocks.
            evt = {**ed, "type": wire.CHECK_VERDICT}
            await self._forward({"pump_type": wire.PUMP_WS_EVENT, "event": evt})
            self._flush_pending_text()
            self._turn_blocks.append(evt)
            if live:
                live["live_blocks"].append(evt)

        elif event.type == PERMISSION_REQUEST:
            self._flush_pending_text()
            self._turn_blocks.append({"type": wire.PERMISSION_PROMPT, **ed})
            # Gate: use the same queue as perm_queue permissions
            perm_data = {**ed, "event_type": wire.ITEM_PERMISSION_PROMPT}
            await self._queue_or_show_permission(perm_data)

        elif event.type == PLAN_MODE:
            evt = {"type": wire.PLAN_MODE, **ed}
            await self._forward(
                {"pump_type": wire.PUMP_WS_EVENT, "event": evt},
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
                    self._submit_chat_update("plan_mode", permission_mode="plan")
            # exit is handled by plan_review_response (implement sets acceptEdits/default)

        elif event.type == TODO_UPDATE:
            # Cross-layer todo/checklist update (TodoWrite for CLI, update_plan for Codex)
            todos = ed.get("todos", [])
            self._current_todos = todos
            await self._forward_event(TODO_UPDATE, {"todos": todos})
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
                        "type": wire.PERSISTED_TOOL, "name": tool_roles.TODO_SNAPSHOT,
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
            await self._forward_event(GOAL_UPDATE, {"goal": goal})
            if live:
                live["goal"] = goal
            if changed and self.chat_id:
                self._submit_chat_update(
                    "thread_goal", thread_goal=json.dumps(goal) if goal else None)

        elif event.type == CONTEXT_COMPACT:
            # Context compression event (CLI auto-compact, Codex compaction, etc.)
            phase = ed.get("phase", "")
            evt = {"type": wire.CONTEXT_COMPACT, **ed}
            await self._forward({"pump_type": wire.PUMP_WS_EVENT, "event": evt})
            if phase == "completed":
                self._flush_pending_text()
                self._turn_blocks.append(evt)
                if live:
                    live["live_blocks"].append(evt)
            elif phase == "usage":
                # Post-compaction gauge state (no chip, not a turn block): a
                # compaction at/after turn end has no follow-up METADATA frame,
                # so the compacted size is persisted here — a reopened chat
                # must not show the pre-compaction gauge.
                post = int(ed.get("post_tokens", 0) or 0)
                if post > 0:
                    self._context_used = post
                    if ed.get("context_max"):
                        self._context_max = int(ed["context_max"])
                    if self.chat_id:
                        self._submit_chat_update("context_used", context_used=post)

        elif event.type == SYSTEM:
            subtype = ed.get("subtype", "")
            # Subagent completion (fg + bg) is a first-class SUBAGENT_END
            # event keyed by tool_use_id (the SubagentStop hook + the
            # task_notification backup) — no FIFO matching. No layer mints
            # a `fg_agents_complete` subtype; that frame's one minter is the
            # liveness clear (ws/dashboard_server_events).
            if subtype == wire.SUBTYPE_MEETING_STARTED:
                evt = {"type": wire.SYSTEM, **ed}
                await self._forward({"pump_type": wire.PUMP_WS_EVENT, "event": evt})
                self._flush_pending_text()
                self._turn_blocks.append(evt)
                # Save immediately so it appears before any QUEUE_TURN user messages
                self._save_turn_blocks()
                if live:
                    live["meeting_participants"] = ed.get("participants", [])
                    # Don't re-add to live_blocks — it's now in DB and will come
                    # via chat_history on reconnect.  Re-adding caused duplicates.
            elif subtype == wire.SUBTYPE_MEETING_TURN_START:
                self._meeting_agent = ed.get("agent", "")
                evt = {"type": wire.SYSTEM, **ed}
                await self._forward({"pump_type": wire.PUMP_WS_EVENT, "event": evt})
                self._flush_pending_text()
                self._turn_blocks.append(evt)
                if live:
                    live["meeting_agent"] = self._meeting_agent
                    live["live_blocks"].append(evt)
            elif subtype == wire.SUBTYPE_MEETING_FAILED:
                # Pre-turn meeting failure (admission denial, spawn failure…)
                # surfaced by the orchestrator: persist immediately so the
                # attach-time chat_history re-send carries the banner, and
                # clear live meeting state so the pill goes away. Not re-added
                # to live_blocks after the save (meeting_started precedent —
                # it would duplicate on reconnect).
                self._meeting_agent = None
                evt = {"type": wire.SYSTEM, **ed}
                await self._forward({"pump_type": wire.PUMP_WS_EVENT, "event": evt})
                self._flush_pending_text()
                self._turn_blocks.append(evt)
                self._save_turn_blocks()
                if live:
                    live["meeting_agent"] = None
                    live["meeting_participants"] = []
            elif subtype in (wire.SUBTYPE_MEETING_TURN_END, wire.SUBTYPE_MEETING_CONCLUDED, wire.SUBTYPE_MEETING_AGENT_FAILED):
                if subtype in (wire.SUBTYPE_MEETING_TURN_END, wire.SUBTYPE_MEETING_CONCLUDED):
                    self._meeting_agent = None
                evt = {"type": wire.SYSTEM, **ed}
                await self._forward({"pump_type": wire.PUMP_WS_EVENT, "event": evt})
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
                    if subtype in (wire.SUBTYPE_MEETING_TURN_END, wire.SUBTYPE_MEETING_CONCLUDED):
                        live["meeting_agent"] = None
                    if subtype == wire.SUBTYPE_MEETING_CONCLUDED:
                        live["meeting_participants"] = []
                    live["live_blocks"].append(evt)
            elif subtype not in wire.SYSTEM_SUBTYPES_TRANSIENT:
                evt = {"type": wire.SYSTEM, **ed}
                await self._forward(
                    {"pump_type": wire.PUMP_WS_EVENT, "event": evt},
                )
                self._flush_pending_text()
                self._turn_blocks.append(evt)
                if live:
                    live["live_blocks"].append(evt)

        elif event.type == METADATA:
            # Persist Codex thread_id for resume after proxy restart
            codex_tid = ed.get("codex_thread_id")
            if codex_tid and self.chat_id:
                self._submit_chat_update("codex_thread_id", codex_thread_id=codex_tid)
                return  # internal metadata — don't forward to WS or save as turn block

            # Display-only flag: the chat hides the cost line + gauge for a
            # subscription / local-model turn. Stamped here, the one place
            # every emitter (CLI, Codex, direct, satellite, meetings) passes
            # through, so it rides the WS frame AND the persisted row. A
            # meeting turn names the speaker's session (each participant
            # runs on its own account); accounting below is untouched.
            cost_billed = await self._cost_billed_for(ed.get("session_id") or self.session_id)
            is_delta = ed.get("cost_is_delta") or ed.get("_meeting_cost")
            if is_delta:
                # Cost is already a per-turn delta (Direct LLM, Codex, meetings).
                # Use directly — don't touch cumulative tracking.
                turn_cost = ed.get("cost_usd", 0)
                meta_event = {"type": wire.METADATA, "cost_usd": turn_cost,
                              "cost_billed": cost_billed}
                # Preserve context/cache/duration fields if present
                for k in ("context_used", "context_max", "cache_read",
                          "cache_write", "input_tokens", "output_tokens",
                          "duration_ms"):
                    if k in ed:
                        meta_event[k] = ed[k]
                await self._forward({"pump_type": wire.PUMP_WS_EVENT, "event": meta_event})
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
                # The baseline made durable for a resume after a proxy restart
                # (the turn-start callback seeds from it: _seed_cost_baseline).
                if self.chat_id:
                    self._submit_chat_update(
                        "engine_cost", engine_cost_total=session_cost,
                        engine_cost_session_id=self.session_id,
                    )
                # Forward per-turn cost (not cumulative)
                meta_event = {**ed, "cost_usd": turn_cost, "cost_billed": cost_billed}
                await self._forward_event(METADATA, meta_event)
                self._flush_pending_text()
                self._turn_blocks.append({"type": wire.METADATA, **meta_event})
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
            await self._forward_event(ERROR, {"message": ed.get("message", "")})

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
            await self._forward({"pump_type": wire.PUMP_IS_DONE})

    async def _handle_perm_event(self, perm_data: dict):
        """Handle a permission queue event — forward to WS or store for re-presentation."""
        evt_type = perm_data.get("event_type", "")
        live = _chat_streaming_state.get(self.chat_id)
        artifact = artifact_events.kind_of(evt_type)

        if artifact is not None:
            # One arm for the eleven artifact kinds (core-seams phase 9); the
            # table in core/events/artifact_events.py says what each does.
            # A block kind (a gallery, a link, a file, a player, a ui page,
            # a preview, a placeholder) is stamped with the meeting speaker,
            # closes the pending text, evicts the latest placeholder of its
            # twin from the turn and live lists and is appended to both; a
            # removal kind (a failed generation or transcode) only evicts.
            # A deferred kind (the document preview) replaces the push of
            # the same file in place and waits for the turn's flush
            # (``_flush_pending_previews``) instead of being forwarded now.
            evt = artifact_event_from_perm_item(perm_data)
            if artifact.block:
                evt = self._stamp_speaker(evt)
                self._flush_pending_text()
            if artifact.evicts:
                _evict_latest(self._turn_blocks, artifact.evicts)
                if live:
                    _evict_latest(live["live_blocks"], artifact.evicts)
            if artifact.deferred:
                self._buffer_preview(evt, artifact.identity, live)
                return
            if artifact.block:
                self._turn_blocks.append(evt)
            await self._forward({"pump_type": wire.PUMP_WS_EVENT, "event": evt})
            if live and artifact.block:
                live["live_blocks"].append(evt)
        elif evt_type == wire.ITEM_TOOL_RESULT:
            tool_name = perm_data["tool_name"]
            result_content = perm_data.get("result_content", "")
            tool_use_id = perm_data.get("tool_use_id", "") or ""
            await self._forward({"pump_type": wire.PUMP_WS_EVENT,
                "event": {"type": wire.TOOL_RESULT,
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
            elif tool_roles.role_of(tool_name) == tool_roles.SUBAGENT and tool_use_id:
                # A FOREGROUND subagent's final report (Agent tools are not
                # tracked in _active_tools — they have dedicated task_spawn
                # blocks). Attach it to the spawn block so the dashboard's
                # subagent pill can expand to the report; same dict ref is in
                # _turn_blocks (persists at DONE) and live_blocks (reconnect).
                # Background spawns are skipped — their PostToolUse result is
                # just the "launched" ack, and the real report arrives via
                # task_notification in a later turn.
                for blk in reversed(self._turn_blocks):
                    if (blk.get("type") == wire.TASK_SPAWN
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
        elif evt_type == wire.ITEM_QUESTION:
            evt = self._stamp_speaker(
                {"type": wire.QUESTION, "tool_name": perm_data["tool_name"],
                 "tool_input": perm_data["tool_input"]})
            self._flush_pending_text()
            self._turn_blocks.append(evt)
            await self._forward({"pump_type": wire.PUMP_WS_EVENT, "event": evt})
            if live:
                live["live_blocks"].append(evt)
        elif evt_type == wire.ITEM_PERMISSION_PROMPT:
            # Gate: only show one blocking prompt at a time
            await self._queue_or_show_permission(perm_data)
        elif evt_type == wire.ITEM_QUESTION_PROMPT:
            # Codex request_user_input — the daemon holds the turn open until
            # question_response resolves it, so this MUST reach the blocking-
            # prompt gate (card + reconnect replay); dropping it hangs the turn.
            await self._queue_or_show_permission(perm_data)
        elif evt_type == wire.ITEM_PLAN_REVIEW:
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
                chat_writer.submit(
                    self.chat_id,
                    functools.partial(task_store.add_chat_plan, self.chat_id,
                                      plan_filename, plan_content),
                    label="chat_plan",
                )
            enriched = {**perm_data, "filename": plan_filename}
            # Save plan_review event for DB history reconstruction
            self._flush_pending_text()
            self._turn_blocks.append({
                "type": wire.PLAN_REVIEW, "request_id": perm_data.get("request_id", ""),
                "plan": plan_content, "tool_input": plan_tool_input,
                "filename": plan_filename,
            })
            # Gate: only show one blocking prompt at a time
            await self._queue_or_show_permission(enriched)
        elif evt_type == wire.ITEM_MODE_RESTORED:
            mode = perm_data.get("mode", "default")
            self._submit_chat_update("mode_restored", permission_mode=mode)
            await self._forward({"pump_type": wire.PUMP_MODE_RESTORED, "mode": mode})


# The background-work monitors live in pump_bg_monitors.py; re-exported here
# so `from core.events.stream_pump import bg_monitor_running` (ws/dashboard) works.
from core.events.pump_bg_monitors import (  # noqa: F401
    bg_monitor_running,
    _bg_agent_monitor,
    bg_command_monitor_running,
    _bg_command_monitor,
)
