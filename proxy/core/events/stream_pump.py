"""ChatStreamPump — decoupled stream processor for dashboard chat turns.

Runs independently of WebSocket connections:
- Reads CommonEvent objects from producer via event queue
- Maintains live streaming state (_chat_streaming_state)
- Saves text and events to DB at turn boundaries
- Optionally forwards events to an attached WS subscriber

Execution-layer agnostic: consumes CommonEvent (not ClaudeStreamChunk).

Module-level state (_active_pumps, _pending_permissions) lives here and is
imported by ws/dashboard.py; _session_cumulative_cost and _GOAL_UNSET are
defined beside the handlers in pump_event_handlers.py and re-exported here
for the tests.
"""

import asyncio
import contextlib
import functools
import json
import logging
import time
import uuid
from datetime import datetime, timezone
from typing import Any, cast

import psycopg

from core.events import (
    artifact_events,
    chat_writer,
    input_queue,
    tool_roles,
    turn_checkpoint,
    turn_ending,
)
from core.events.artifact_events import artifact_event_from_perm_item
from core.events.pump_event_handlers import (  # _GOAL_UNSET and the cost map are re-exported here for the tests
    _GOAL_UNSET,
    PumpEventHandlers,
    _session_cumulative_cost,
)
from services.notifications import notification_manager
from storage import database as task_store
from storage.automation import run_status
from storage.chat import open_turns
from storage.pg import run_db
from ws import chat_phase, wire_events as wire

#: The platform's own delegate MCP tool: its call renders as the delegate
#: pill from the proxy's ``delegate_spawn`` frame, never as a tool block
#: (the dashboard's skip set carries the same name — ``lib/tools/roles.ts``).
DELEGATE_TOOL = "mcp__delegation-mcp__delegate"
from core.background import spawn
from core.events.bg_command_state import get_bg_command_registry
from core.events.common_events import (
    ARTIFACT_TURN,
    BG_REVIEW_TURN,
    ERROR,
    PRODUCER_DONE,
    QUEUE_TURN,
    TEXT,
    THINKING,
    WORKFLOW_PROGRESS,
    CommonEvent,
    TurnInput,
)
from core.permission_modes import ASK_MODE
from core.session import session_kind
from core.session.session_state import (
    _chat_streaming_state,
    get_subagent_registry,
    prompt_waits,
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
from services.title_generator import (  # noqa: F401  # read by the handlers' module; a test imports the tool one from here
    TITLE_CHAR_THRESHOLD as _TITLE_CHAR_THRESHOLD,
    TITLE_TOOL_THRESHOLD as _TITLE_TOOL_THRESHOLD,
)

logger = logging.getLogger("otodock")

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
# A card retired because its hook caller went away is followed by the
# hook's retry of the same call (hooks/permission_gate.py: a stream that
# died silently is noticed at its 150 s read timeout, then retried with
# backoff): the retry's card shows without a second ping until this long
# after the retire; a later retry pings again.
_RETRY_QUIET_S = 180.0
# The drivers whose turns never ping for a held prompt: a task run reports
# by its notification mode, a meeting turn is not the person's own.
_NO_PING_DRIVERS = (session_kind.TASK.source_type, session_kind.MEETING.source_type)


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
    a TEXT that is only content (a note only with a note), a THINKING text
    delta (same other keys), a THINKING progress ping (latest wins)."""
    if event_type == TEXT:
        return (TEXT, bool(payload.get("note"))) if payload.keys() <= {"content", "note"} else None
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

# A pump-internal event (never a layer's): a message the engine took into
# the running turn, put on the pump's own event queue by ``record_steer`` so
# its row lands in stream order. ``{"item": QueuedInput, "extra_meta",
# "frame_extra"}``.
STEER_ACCEPTED = "steer_accepted"

# Active stream pumps per chat — decoupled from WebSocket connections.
# When a WS detaches (chat switch, browser close), the pump keeps running.
# When a WS reconnects, it attaches to the existing pump for live updates.
_active_pumps: dict[str, "ChatStreamPump"] = {}  # chat_id -> pump

# Chats whose in-flight turn will be RE-ADOPTED from the satellite after a
# proxy restart (Mode C). During graceful shutdown the pump must NOT persist
# the partial turn's blocks or cost — the recovery replay re-persists the
# full turn, so a shutdown flush would duplicate content + double-count cost.
_recovery_suppress_flush: set[str] = set()  # chat_id

# The platform is ending (the lifespan's shutdown began): a turn whose pump
# ends now without its engine's result or a typed ending was cut by the
# restart, and its open-turn record stays for the boot's closing row.
_platform_ending = False


def note_platform_ending() -> None:
    """The first step of the graceful shutdown (``startup.lifespan``)."""
    global _platform_ending
    _platform_ending = True


def pending_preview_snapshot(chat_id: str, file_id: str) -> str | None:
    """The snapshot of a document push the chat's pump holds for the turn's
    flush (pushed this turn, not yet persisted), None when there is none. A
    save from the document pane refreshes that version rather than the
    file's previous one. Read on the loop."""
    pump = _active_pumps.get(chat_id)
    if pump is None:
        return None
    evt = pump._pending_previews.get(file_id)
    return (evt or {}).get("snapshot_id") or None


def suppress_recovery_flush(chat_id: str) -> None:
    """Mark a chat's in-flight turn for satellite re-adopt: its pump skips the
    durable turn-block + cost persist so the post-restart replay doesn't
    duplicate them, and leaves the turn's open-turn record. The rows a steer
    or a checkpoint saved early stay: their ranges are in that record, and the
    adoption deletes them before the replay writes the whole turn
    (``run_recovery._recover_session``); a turn nothing replays keeps them,
    with the boot's closing row."""
    _recovery_suppress_flush.add(chat_id)


def _evict_latest(blocks: list[dict], kind: str) -> None:
    """Remove the LATEST block of ``kind`` (a placeholder the real artifact
    or its failure replaces) from a block list, in place."""
    for i in range(len(blocks) - 1, -1, -1):
        if blocks[i].get("type") == kind:
            blocks.pop(i)
            return


def undelivered_card(items: "list[TurnInput]", *, reason: str = wire.UNDELIVERED_QUEUED,
                     ) -> dict | None:
    """The undelivered-input card of queued messages nothing will send: the
    ``system`` block a ``reason: queued`` row stores (and a viewing socket is
    sent live), the texts joined as ``queue_cleared`` joins them and the
    attachments named. None when there is nothing to show."""
    text = "\n\n".join(i.text for i in items if i.text)
    names = [m.get("name") or (m.get("path") or "").rsplit("/", 1)[-1]
             for i in items for m in (*i.image_meta, *i.files)]
    names = [n for n in names if n]
    if names:
        text = (text + "\n\n" if text else "") + "Attached: " + ", ".join(names)
    if not text:
        return None
    return {"type": wire.SYSTEM, "subtype": wire.SUBTYPE_UNDELIVERED_INPUT,
            "reason": reason, "message": text}


def shelve_undelivered(chat_id: str, items: "list[TurnInput]", *,
                       reason: str = wire.UNDELIVERED_QUEUED) -> None:
    """Queued messages nothing will send, kept in their chat as one
    undelivered-input card (the system row an abandoned terminal flush writes
    too) on the chat's writer lane, so the person finds the text there and
    sends it again (``undelivered_card``). ``reason`` says why (the wire's
    ``UNDELIVERED_*`` words)."""
    if not chat_id or not items:
        return
    block = undelivered_card(items, reason=reason)
    if block is None:
        return
    chat_writer.submit(
        chat_id,
        functools.partial(task_store.add_chat_message, chat_id, "event", "",
                          event_type=wire.SYSTEM, event_data=json.dumps(block)),
        label="undelivered_input",
    )
    logger.info("chat=%s: %s queued message(s) nothing sent, "
                "kept as an undelivered-input card", chat_id[:8], len(items))


def turn_ended_block(ending: "turn_ending.TurnEnding") -> dict:
    """The ``system`` block of a turn's typed ending (subtype
    ``turn_ended``): the reason, the line the card shows, the engine's
    detail, a limit's reset and an exited process's code."""
    block: dict[str, Any] = {"type": wire.SYSTEM, "subtype": wire.SUBTYPE_TURN_ENDED,
             "reason": ending.reason, "message": ending.line(),
             "detail": ending.detail, "resets_at": ending.resets_at}
    if ending.exit_code is not None:
        block["exit_code"] = ending.exit_code
    return block


#: Chats holding a wake stored while another turn had them: the next pump's
#: end on the chat replays it (``_replay_stored_wakes``), so a wake refused
#: by a phone, voice or meeting turn is not left for the next warmup.
_stored_wake_chats: set[str] = set()


def note_stored_wake(chat_id: str) -> None:
    """A wake was stored on ``chat_id``: its next pump end replays it."""
    _stored_wake_chats.add(chat_id)


async def _replay_stored_wakes(chat_id: str) -> None:
    """The stored wakes of a chat whose turn just ended, through the
    delivery's sweep of one chat (its starters ask the chat's claim; a
    refused one stores the wake again for the next pump end)."""
    from services.scheduler import delivery
    try:
        chat = await run_db(task_store.get_chat, chat_id)
        if chat:
            await delivery._redeliver_chat(chat)
    except Exception:
        logger.exception("chat=%s: the stored wakes' replay failed", chat_id[:8])


_replays: set[asyncio.Task] = set()


def _spawn_replay(chat_id: str) -> None:
    task = asyncio.create_task(_replay_stored_wakes(chat_id))
    _replays.add(task)
    task.add_done_callback(_replays.discard)


# The "needs your input" pings in flight (``ChatStreamPump._ping_needs_input``).
_pings: set[asyncio.Task] = set()


def _call_key(perm_data: dict) -> str:
    """One tool call's identity across the hook's retries: its name and input."""
    return f"{perm_data.get('tool_name') or ''}\0" + json.dumps(
        perm_data.get("tool_input") or {}, sort_keys=True, default=str)


def store_chat_wake(chat_id: str, prompt: str, person: str = "") -> bool:
    """Store ``prompt`` as the chat's durable wake for ``person``, the person
    the chat's turn runs as, at their role on the chat's agent now; "" names
    no person. The redelivery sweep runs a stored person's wake as them
    (TASKS.md "Stored wakes"). True when stored. Synchronous: call it on the
    DB executor or the chat's writer lane."""
    role = ""
    if person:
        from auth.providers import acting_role_of
        agent = (task_store.get_chat(chat_id) or {}).get("agent") or ""
        role = acting_role_of(person, agent)
    stored = bool(task_store.append_pending_delegate_wake(
        chat_id, prompt, person=person, role=role))
    if stored:
        note_stored_wake(chat_id)
    return stored


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
            meta: dict = {"note": True} if block.get("note") else {}
            if meeting_agent:
                from storage.agents import agent_store
                ad = agent_store.get_agent(meeting_agent)
                meta.update({
                    "agent_slug": meeting_agent,
                    "agent_display_name": (ad or {}).get("display_name", meeting_agent),
                    "agent_color": (ad or {}).get("color", ""),
                    "badge": "meeting",
                })
            rows.append(("assistant", block["content"], "", json.dumps(meta) if meta else ""))
        else:
            stored = block
            if artifact is not None and any(k in block for k in artifact_events.LIVE_ONLY_KEYS):
                stored = {k: v for k, v in block.items() if k not in artifact_events.LIVE_ONLY_KEYS}
            rows.append(("event", "", block["type"], json.dumps(stored)))
    return rows


def _stored_binding(session_id: str) -> dict | None:
    """The session's persisted pool binding, None when it has none or the
    read failed (the usage then goes to ``default``, as before)."""
    from storage.billing import subscription_store
    try:
        row = subscription_store.get_session_binding(session_id)
    except psycopg.Error:
        logger.debug("usage snapshot: subscription lookup failed", exc_info=True)
        return None
    return row if isinstance(row, dict) else None


def _record_usage(chat_row: dict, snap: dict) -> None:
    """Record one usage_records row for the LLM + one per (provider, model)
    for any per-tool MCP costs accumulated this turn — from the cost job, on
    the DB executor, with the pump's values snapshotted on the loop.

    The LLM row is always written (even at $0 cost) so token counts and the
    message_count survive — analytics depends on them. MCP rows are only
    written when their cost is positive.
    """
    try:
        from config import get_model_provider
        from services.billing import usage_service

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
    except (ImportError, psycopg.Error) as e:
        logger.error("Failed to record usage: %s", e)


class ChatStreamPump(PumpEventHandlers):
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
        driver_tells_parked: bool = False,
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
        # The driver tells the person itself when the turn parks on a prompt
        # nobody watches (the delegate echo's watchdog): no ping of our own
        # while no viewer is attached.
        self.driver_tells_parked = driver_tells_parked

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
        # The typed ending of the turn (every way it ended short: a decline,
        # a limit, an error, an exit, a silence, a loss); the runner and the
        # delegator read it.
        self.last_ending: turn_ending.TurnEnding | None = None

        # Spoken utterances and the plan's implement message (never a typed
        # message: those wait in the chat's own queue, input_queue.py), shared
        # with a producer that drains them as its next turn while the queue
        # is open (``queue_message``); one left at the close is kept in the
        # chat as an undelivered card.
        self.message_queue: list[TurnInput] = []
        self._leftover_messages: list[TurnInput] = []
        # Accept jobs this pump has in flight (a steer's row, a drained
        # batch's rows, a run's review rows): a resume drains the chat's lane
        # before its cut.
        self.accepts_in_flight = 0
        # The person pressed Stop (``ws/dashboard_dispatch._on_abort``): the
        # queue was returned there, nothing is returned at the end.
        self.stopped = False
        # The chat is being deleted (``end_for_delete``): the turn ends now,
        # no alert fires, no title is asked, the queue goes to nobody.
        self.deleting = False
        # The queue sequence at this pump's failure: the messages queued up
        # to it are returned at its end, later ones go out as the next turn.
        self._return_upto: int | None = None
        # A satellite turn the proxy may re-adopt after a restart: the id
        # ranges a steer's early save wrote, since the current engine turn
        # began (the turn's open-turn record holds every early save's).
        self._recovery_eligible: bool | None = None
        self._steer_saved_ranges: list[tuple[int, int]] = []
        # The turn's open-turn record (``storage/chat/open_turns.py``): the
        # token the start wrote (only the pump registered for the chat
        # writes one), deleted by that token at the end.
        self._turn_token = ""
        # The engine's result (DONE) arrived in the current engine turn.
        self._engine_done = False
        # Checkpoints (``turn_checkpoint.py``): the loop time of the last
        # save, the last checkpoint's job, the live twins, the checklist's
        # row, the ids of the live blocks checkpoints in flight will trim
        # (new words never go into one), the foreground subagents whose
        # spawn is done, the plan reviews answered (when), a search row owed.
        self._last_save_at: float | None = None
        self._checkpoint_fut: asyncio.Future | None = None
        self._live_twins: turn_checkpoint.Twins = {}
        self._todo_ref: turn_checkpoint.RowRef | None = None
        self._checkpoint_trims: set[int] = set()
        self._spawns_done: set[str] = set()
        self._reviews_answered: dict[str, float] = {}
        self._search_owed = False
        # System prompt queue — delivered silently (no user bubble).
        # Used for delegate results and bg nudges during background drain.
        self.system_queue: list[str] = []
        # True only when a producer actually DRAINS system_queue (dashboard
        # turn loop, duplex, meetings). Task/scheduler pumps never do — the
        # delivery ladder must not "succeed" onto a queue nobody reads
        # (queue_pump_prompt checks this flag).
        self.system_queue_consumer: bool = False
        # The person this pump's turns run as, set by a producer that runs
        # as one (the dashboard and duplex turns): a system prompt left on
        # the queue at close is stored as their wake. "" stores no person.
        self.wake_person: str = ""
        # Artifact-interaction queue (shared with producer closure) — pending
        # otodock.send payloads from display_ui artifacts, delivered as their
        # own framed turn(s) at the boundary (ws/artifact_interactions.py).
        self.artifact_queue: list[dict] = []
        # The artifact queue accepts only while a producer still drains it
        # (a typed message waits in the chat's own queue instead,
        # core/events/input_queue.py). A producer that drains it opens it
        # when it shares its list; a pump nothing drains (task run,
        # recovery, phone, delegate echo) keeps it closed, so queue_artifact
        # refuses instead of acknowledging an interaction nobody sends.
        # close_queue moves what it holds to the leftover list a viewer
        # takes once.
        self.queue_closed: bool = True
        self.artifact_leftover: list[dict] = []

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
        self._pending_note = False  # the pending segment is a progress note (TEXT ``note``)
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
        # The session's stored pool binding, read by the turn-start job when
        # the pool's memory did not know the session (``_usage_snapshot``).
        self._stored_binding: dict | None = None
        self._binding_read = False
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
        # The "needs your input" ping: the cards shown with one (or quietly,
        # as a retried call's card) by request id -> call key, and the calls
        # whose pinged card was retired with its caller gone, key -> when.
        self._pinged: dict[str, str] = {}
        self._retried: dict[str, float] = {}

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

    @property
    def has_viewers(self) -> bool:
        """Whether any consumer (a dashboard viewer, a phone or duplex
        stream) is attached to the pump now."""
        return bool(self._ws_queues)

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
            self._ws_queues.remove(cast("_ViewerQueue", q))

    async def end_for_delete(self, timeout: float = 5.0) -> None:
        """The chat is being deleted: the turn ends now and the pump's rows
        and turn-end job are queued on the chat's lane before the route
        queues the row delete. Stopped and deleting (no alert, no title,
        the queue returned to nobody: its rows go with the chat), the
        session told (a settling run ends), the interactions and system
        prompts dropped, the producer ended on the hard path (the session
        is closed right after), then the pump's task awaited within
        ``timeout``. A pump already done but still unwinding is awaited the
        same way."""
        self.stopped = True
        self.deleting = True
        self._title_armed = False
        if self.session_id:
            from core.session import session_events
            session_events.note_stop(self.session_id)
        self.cancel_all_artifacts()
        self.message_queue.clear()
        self.system_queue.clear()
        if not self._done:
            self.abort()
        if self._task is not None and not self._task.done():
            ended, _ = await asyncio.wait({self._task}, timeout=timeout)
            if not ended:
                logger.warning(
                    "chat=%s: the pump did not end within %.0fs of the chat's delete; "
                    "the row delete goes ahead of its last writes", self.chat_id[:8], timeout)

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
        if self._permission_active is not None:
            rid = self._permission_active.get("request_id") or ""
            self._pinged.pop(rid, None)
            if self._permission_active.get("event_type") == wire.ITEM_PLAN_REVIEW:
                self._reviews_answered[rid] = time.monotonic()
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

    async def _retire_prompt(self, request_id: str, *, caller_gone: bool) -> None:
        """A prompt's wait ended with no answer (``api/hooks/permission``
        ``_wait_on_person``): it leaves the buffer, the viewers drop its card,
        and when it holds the slot the next prompt shows. A plan review whose
        caller went away leaves the turn's blocks too, since the retried call
        raises its own; one that ran out or was released stays the
        transcript's record of the review."""
        if not request_id:
            return
        key = self._pinged.pop(request_id, None)
        if caller_gone and key is not None:
            self._retried[key] = time.monotonic()
        self._permission_buffer[:] = [
            p for p in self._permission_buffer if p.get("request_id") != request_id]
        if caller_gone:
            # Only the turn's unsaved blocks: a plan review is never a live
            # block (its card rides pending_permission).
            self._turn_blocks[:] = [
                b for b in self._turn_blocks
                if not (b.get("type") == wire.PLAN_REVIEW and b.get("request_id") == request_id)]
        await self._forward({"pump_type": wire.PUMP_WS_EVENT,
                             "event": {"type": wire.PROMPT_RETIRED, "request_id": request_id}})
        active = self._permission_active
        if active is not None and active.get("request_id") == request_id:
            await self.resolve_active_permission()

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
        self._pinged.clear()
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
            # The plan waits on the person like a permission does.
            self._ping_needs_input(perm_data, "Review the plan")
        elif evt_type == wire.ITEM_QUESTION_PROMPT:
            # Codex request_user_input: the daemon HOLDS the turn open on this
            # question, so the turn-end ping never fires for it. Do NOT touch
            # chat_status — the turn is still streaming (no "ready" flip
            # mid-held-turn).
            await self._forward(
                {"pump_type": wire.PUMP_QUESTION_PROMPT, "perm_data": perm_data},
            )
            qs = (perm_data.get("tool_input") or {}).get("questions") or []
            first_q = (qs[0].get("question") if qs and isinstance(qs[0], dict)
                       else "") or "Waiting for your answer"
            self._ping_needs_input(perm_data, first_q)
        else:
            forward_data = {"pump_type": wire.PUMP_PERMISSION_PROMPT, "perm_data": perm_data}
            # Include meeting agent identity so the frontend can show which agent is asking
            if perm_data.get("meeting_agent"):
                forward_data["meeting_agent"] = perm_data["meeting_agent"]
            await self._forward(forward_data)
            # The tool's input stays out of the body: it rides the push to
            # the phone, and a command or a URL can carry a secret.
            self._ping_needs_input(perm_data, f"Allow {perm_data.get('tool_name') or 'a tool'}?")

    def _ping_needs_input(self, perm_data: dict, body: str) -> None:
        """A held prompt just shown pings its person: the "needs your input"
        ephemeral (away and FCM aware), the only signal of a card that waits
        mid-turn (the turn-end ping waits for the turn). Never for a task or
        meeting turn, a meeting participant's card, a driver that sends its
        own parked notice, a card whose wait already ended (a dropped
        connection retires several at once, and retiring the active one shows
        the next for a moment), or the retry of a hook call whose first card
        pinged before its caller went away. Synchronous up to the spawn: the
        card is never held by the chat row read."""
        rid = str(perm_data.get("request_id") or "")
        if (not rid or (self.driver_tells_parked and not self.has_viewers)
                or perm_data.get("meeting_agent")
                or self.source_type in _NO_PING_DRIVERS or not prompt_waits(rid)):
            return
        key = _call_key(perm_data)
        now = time.monotonic()
        for k, at in list(self._retried.items()):
            if now - at > _RETRY_QUIET_S:
                del self._retried[k]
        self._pinged[rid] = key
        if self._retried.pop(key, None) is not None:
            return
        task = asyncio.create_task(self._send_ping(rid, key, body))
        _pings.add(task)
        task.add_done_callback(_pings.discard)

    async def _send_ping(self, request_id: str, key: str, body: str) -> None:
        sent = False
        try:
            sent = await self._fire_ping(request_id, body)
        finally:
            if not sent:
                # Nothing reached the person, so the call's retry must not
                # stay quiet: before its retire the card is forgotten, after
                # it the retire's entry goes.
                if self._pinged.pop(request_id, None) is None:
                    self._retried.pop(key, None)

    async def _fire_ping(self, request_id: str, body: str) -> bool:
        chat_id = self.chat_id

        def _row_meeting_label() -> tuple[dict | None, bool, str]:
            row = task_store.get_chat(chat_id)
            if not row:
                return None, False, ""
            return (
                row,
                bool(task_store.get_active_meeting_for_chat(chat_id)),
                notification_manager.agent_label(row["agent"]) if row.get("agent") else "",
            )

        try:
            chat, in_meeting, label = await run_db(_row_meeting_label)
        except psycopg.Error:
            logger.debug("chat=%s: the needs-input ping's chat read failed", chat_id[:8],
                         exc_info=True)
            return False
        # Answered or released while the row was read: nothing waits.
        if not chat or in_meeting or not prompt_waits(request_id):
            return False
        await notification_manager.fire_ephemeral(
            chat["user_sub"],
            title=f"{label or chat.get('agent') or ''} needs your input",
            body=body,
            chat_id=chat_id,
            mid_turn=True,
        )
        return True

    async def _queue_or_show_permission(self, perm_data: dict):
        """Show permission if none active, otherwise buffer it."""
        if self._permission_active is None:
            await self._show_permission(perm_data)
        else:
            self._permission_buffer.append(perm_data)

    # Cap the utterance backlog per pump (spoken turns arrive one by one).
    _QUEUE_CAP = 64
    # queue_message's refusal of a closed queue (-1 is the cap).
    QUEUE_CLOSED = -2

    def queue_message(self, item: TurnInput) -> int:
        """Queue a spoken utterance (or the plan's implement message) for the producer's
        next turn. Returns the index, -1 at the cap, ``QUEUE_CLOSED`` when
        nothing drains the queue any more. A typed message never comes here:
        it waits in the chat's queue (``core/events/input_queue.py``)."""
        if self.queue_closed:
            return self.QUEUE_CLOSED
        if len(self.message_queue) >= self._QUEUE_CAP:
            return -1
        self.message_queue.append(item)
        return len(self.message_queue) - 1

    def record_steer(self, qi: "input_queue.QueuedInput", *,
                     extra_meta: dict | None = None, frame_extra: dict | None = None) -> bool:
        """A message the engine took into the running turn, recorded in
        stream order: the event goes on the pump's own queue, so the blocks
        the engine produced before it are saved first and its user row lands
        after them, then ``steered`` with the row's id reaches the attached
        viewers at that point of the stream. False when the pump is done (the
        caller writes the row on the lane and fans the frame out)."""
        if self._done:
            return False
        self.accepts_in_flight += 1
        self.event_queue.put_nowait(CommonEvent(type=STEER_ACCEPTED, data={
            "item": qi, "extra_meta": extra_meta, "frame_extra": frame_extra,
        }))
        return True

    async def _accept_steer(self, data: dict) -> None:
        """The STEER_ACCEPTED handler: the text so far flushed and the blocks
        saved, the steer's row awaited on the lane after them, then the
        frame forwarded in place."""
        qi = data["item"]
        try:
            self._flush_pending_text()
            # Previews held for the turn's end are in the blocks this save
            # writes: the dashboard gets them now, ahead of the steer.
            await self._flush_pending_previews()
            if self._recovery_eligible is None:
                from services.scheduler import run_recovery
                self._recovery_eligible = bool(await run_db(
                    run_recovery.is_recovery_eligible, self.chat_id))
            self._save_turn_blocks(note_range=self._recovery_eligible, early=True)
            ids = await input_queue.get(self.chat_id).accept_job(
                [qi], extra_meta=data.get("extra_meta"))
        finally:
            self.accepts_in_flight -= 1
        await self._forward({"pump_type": wire.PUMP_WS_EVENT,
                             "event": input_queue.steered_frame(
                                 self.chat_id, qi, ids, data.get("frame_extra"))})

    async def _accept_queue_turn(self, data: dict) -> None:
        """The QUEUE_TURN handler: the producer drained the chat's queue (or
        its spoken utterances) as its next turn. The blocks so far are saved
        first, so the user rows land after them in DB order (the previous
        turn's tail, a meeting turn's content), then the accept job, one
        user row per message with its attachments and, in a meeting, the
        moderator's badge. The producer waits on ``accepted`` before the
        engine gets the batch, so a crash in between leaves the rows
        queued, never answered twice. A nudge's ``text`` is a row of its
        own."""
        self._flush_pending_text()
        if self._turn_blocks:
            self._save_turn_blocks()
        # The restart replay covers the engine turn starting now only.
        self._steer_saved_ranges = []
        self._engine_turn_begins()
        inputs = list(data.get("inputs") or [])
        if not inputs and data.get("text"):
            meta = data.get("event_data") or {}
            inputs = [input_queue.QueuedInput(
                queue_id="", chat_id=self.chat_id, author_sub="",
                item=TurnInput(data["text"], image_meta=list(meta.get("images") or []),
                               files=list(meta.get("files") or [])))]
        meeting_agent = data.get("meeting_agent")
        extra_meta: dict | None = None
        if meeting_agent:
            from storage.agents import agent_store as _agent_store
            ad = _agent_store.get_agent(meeting_agent)
            extra_meta = {
                "agent_slug": meeting_agent,
                "agent_display_name": (ad or {}).get("display_name", meeting_agent),
                "agent_color": (ad or {}).get("color", ""),
                "badge": "meeting prompt",
            }
        accepted = data.get("accepted")
        self.accepts_in_flight += 1
        try:
            q = await input_queue.loaded(self.chat_id)
            ids = await q.accept_job(inputs, extra_meta=extra_meta)
        except Exception as e:
            if accepted is not None and not accepted.done():
                accepted.set_exception(e)
            raise
        finally:
            self.accepts_in_flight -= 1
        if accepted is not None and not accepted.done():
            accepted.set_result(ids)
        batch = TurnInput.combine([qi.item for qi in inputs])
        # The live echo to every socket of the chat's audience and of the
        # authors (the attached viewer among them): the joined bubble now,
        # one bubble per row after a reload.
        input_queue.fan_out(self.chat_id, {
            "type": wire.QUEUE_SENT,
            "queue_ids": [qi.queue_id for qi in inputs if qi.queue_id],
            "message_ids": ids, "text": batch.text, **batch.frame_fields(),
        }, authors={qi.author_sub for qi in inputs})

    def cancel_all_queued(self, reason: str = "revoked") -> None:
        """Everything queued behind this pump's turn, for a caller that stops
        it outside a dashboard socket (offboarding): the turn is stopped,
        the interactions and the utterances go, and the chat's queue goes
        back to its authors."""
        self.stopped = True
        self.cancel_all_artifacts()
        self.message_queue.clear()
        input_queue.return_soon(self.chat_id, reason)

    def cancel_all_artifacts(self) -> None:
        """Drop the queued artifact interactions, a closed queue's leftover
        included (they were never delivered, so nothing persists)."""
        self.artifact_queue.clear()
        self.artifact_leftover.clear()

    def queue_artifact(self, interaction: dict) -> bool:
        """Queue an artifact interaction for the boundary drain. False when
        the queue is closed or the pending cap is hit (each delivery costs a
        real agent turn)."""
        from core.events.artifact_events import QUEUE_CAP
        if self.queue_closed or len(self.artifact_queue) >= QUEUE_CAP:
            return False
        self.artifact_queue.append(interaction)
        return True

    def close_queue(self) -> None:
        """Nothing drains the queues from here on: queue_message and
        queue_artifact refuse, an utterance still queued is kept for the
        pump's end (an undelivered card), and an interaction moves to the
        leftover list (``take_artifact_leftover``). A typed message is the
        chat's own queue's, delivered at this pump's end. A system prompt
        still queued is not a viewer's to send: it goes to the chat's
        durable wake (for ``wake_person``), which the chat's next turn
        replays, and ``system_queue_consumer`` drops so the delivery ladder
        takes its other rungs. Idempotent."""
        self.queue_closed = True
        self.system_queue_consumer = False
        self._leftover_messages.extend(self.message_queue)
        self.message_queue.clear()
        self.artifact_leftover.extend(self.artifact_queue)
        self.artifact_queue.clear()
        for text in self.system_queue:
            chat_writer.submit(
                self.chat_id,
                functools.partial(store_chat_wake, self.chat_id, text, self.wake_person),
                label="queue_wake",
            )
        self.system_queue.clear()

    def take_artifact_leftover(self) -> list[dict]:
        """What the closed artifact queue held, once, in queue order."""
        arts = list(self.artifact_leftover)
        self.artifact_leftover.clear()
        return arts

    def _flush_pending_text(self):
        """Flush accumulated text to _turn_blocks as a text segment.

        Captures the current _meeting_agent so text blocks retain agent
        identity even when saved later (e.g. at PRODUCER_DONE when
        _meeting_agent has already been cleared by meeting_concluded).
        """
        text = "".join(self._pending_text)
        if text:
            block = self._stamp_speaker({"type": wire.TEXT, "content": text})
            if self._pending_note:
                block["note"] = True
            self._turn_blocks.append(block)
            live = _chat_streaming_state.get(self.chat_id)
            shown = live.get("live_blocks") if live else None
            if shown and shown[-1].get("type") == wire.LIVE_TEXT:
                self._live_twins[id(block)] = (block, shown[-1])
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

    def _rewrite_row(self, ref: turn_checkpoint.RowRef, block: dict) -> None:
        """The row a checkpoint saved for ``block`` rewritten as it is now."""
        turn_checkpoint.rewrite_row(self.chat_id, ref, _serialize_turn_rows([block])[0][3])

    async def _maybe_checkpoint(self) -> None:
        """A long turn's settled blocks saved while it runs (``turn_checkpoint.due_upto``)."""
        upto = turn_checkpoint.due_upto(self, suppressed=self.chat_id in _recovery_suppress_flush)
        if upto:
            self._checkpoint_fut = self._save_turn_blocks(upto=upto)

    def _engine_turn_begins(self) -> None:
        """A new engine turn inside this pump's turn (its ranges start over)."""
        self._engine_done = False
        turn_checkpoint.note_engine_turn(self.chat_id, self._turn_token, running=True)

    def _engine_turn_ended(self) -> None:
        """The engine's result arrived, after the DONE save: the rows are final."""
        self._engine_done = True
        turn_checkpoint.note_engine_turn(self.chat_id, self._turn_token, running=False)

    def _save_turn_blocks(self, *, note_range: bool = False, early: bool = False,
                          upto: int | None = None) -> asyncio.Future | None:
        """Persist the turn blocks accumulated so far, in order, off the loop.

        The rows are serialised HERE (the block dicts keep mutating on the
        loop afterwards — plan_review actions, live badges), the saved blocks
        leave the accumulator, and ONE writer job per call goes on the chat's
        lane: inserts → snapshot GC → the last message id. When the job lands
        the id-based cutoff advances and exactly the live blocks it persisted
        are trimmed from live_state (``turn_checkpoint.trim_list``, taken now)
        — never neither, never both for a viewer reconnecting. Returns the
        job's future, or None when nothing was queued. ``note_range``: the
        pump records the id range (a steer's save on a turn a restart may
        replay); ``early`` (a steer, a checkpoint): the open-turn record gets
        it inside the rows' transaction. ``upto`` makes it a checkpoint: the
        first ``upto`` blocks (``settled_prefix``), the row-less ones kept in
        front, the checklist segment going on, no search-row rebuild.
        """
        if self.chat_id in _recovery_suppress_flush:
            # This turn will be re-adopted + re-persisted from the satellite
            # after the restart — a shutdown flush here would duplicate it.
            self._turn_blocks.clear()
            return None
        checkpoint = upto is not None
        if not checkpoint:
            # The segment ends here, saved or already checkpointed: a
            # checklist after it is a new block.
            self._todo_block = self._todo_ref = None
        prefix = self._turn_blocks[:upto]
        saved = [b for b in prefix if turn_checkpoint.rowed(b)] if checkpoint else prefix
        if not saved:
            if not checkpoint and self._search_owed:
                # Nothing left to write: the checkpoints' text still owes
                # the chat's search row.
                self._search_owed = False
                chat_writer.submit(self.chat_id, functools.partial(
                    task_store.rebuild_chat_search, self.chat_id), label="search_row")
            return None
        rows = _serialize_turn_rows(saved)
        self._turn_blocks[:len(prefix)] = [b for b in prefix if not turn_checkpoint.rowed(b)] if checkpoint else []
        ref = turn_checkpoint.todo_ref(saved, self._todo_block) if checkpoint else None
        if ref is not None:
            self._todo_ref = ref[1]
        # A tool still open is not in these rows: its live block stays, and
        # it lands at its start position in the blocks that follow.
        open_tools = list(self._active_tools.values())
        for b in open_tools:
            b["_insert_idx"] = b["_insert_idx"] - len(saved) if checkpoint else 0
        # A checkpoint runs no snapshot GC (a held preview is not saved yet)
        # and leaves the search row to the next full save.
        gc_pending = search_owed = False
        if checkpoint:
            self._search_owed = True
        else:
            gc_pending, self._gc_snapshots_pending = self._gc_snapshots_pending, False
            search_owed, self._search_owed = self._search_owed, False
        chat_id = self.chat_id
        live = _chat_streaming_state.get(chat_id)
        live_list = live.get("live_blocks") if live else None
        trim = turn_checkpoint.trim_list(live_list or [], self._turn_blocks, open_tools,
                                         self._live_twins)
        trim_ids = {id(b) for b in trim}
        if checkpoint:
            self._checkpoint_trims |= trim_ids
            for b in saved:
                self._live_twins.pop(id(b), None)
        else:
            self._live_twins.clear()
        ranges = self._steer_saved_ranges if note_range else None
        token = self._turn_token if early or checkpoint else ""
        self._last_save_at = asyncio.get_running_loop().time()

        def _job() -> int:
            before = task_store.get_last_chat_message_id(chat_id) if ranges is not None or token else 0
            ids: list[int] = []

            def _note(conn: psycopg.Connection, written: list[int]) -> None:
                if max(written, default=0):
                    open_turns.note_range_in(conn, chat_id, token, before or 0, max(written))

            # The row ids for a row rewritten later, the range note, the
            # search row left to the next full save: none on the plain save.
            extra: dict[str, Any] = {"ids": ids} if ref is not None else {}
            if token:
                extra["in_txn"] = _note
            if checkpoint:
                extra["search"] = False
            # One transaction and ONE search-row rebuild for the turn's rows.
            task_store.add_chat_messages_batch(chat_id, [
                ("assistant", content, "", event_data) if role == "assistant"
                else ("event", "", event_type, event_data)
                for role, content, event_type, event_data in rows
            ], **extra)
            if ref is not None:
                ref[1].id = ids[ref[0]] if ref[0] < len(ids) else 0
            if search_owed and not any(role == "assistant" and content for role, content, _t, _d in rows):
                task_store.rebuild_chat_search(chat_id)
            if gc_pending:
                # The flushed preview rows are persisted now — prune this
                # chat's unreferenced version-pinned snapshots (dismissed
                # history etc.).
                try:
                    from services.media import preview_snapshots
                    preview_snapshots.gc_chat(chat_id)
                except (ImportError, OSError, psycopg.Error):
                    logger.debug("preview snapshot GC failed", exc_info=True)
            last = task_store.get_last_chat_message_id(chat_id)
            if ranges is not None:
                ranges.append((before or 0, last or 0))
            return last

        fut = chat_writer.submit(chat_id, _job, label="turn_blocks")

        def _landed(f: asyncio.Future) -> None:
            if checkpoint:
                self._checkpoint_trims -= trim_ids
            if f.cancelled() or f.exception() is not None:
                return
            # Advance the cutoff so the saved content (completed meeting
            # turns included) comes from chat_history on reconnect, not
            # truncated as in-flight.
            self._db_msg_cutoff_id = max(self._db_msg_cutoff_id or 0, f.result() or 0)
            # Trim exactly the blocks this job persisted; blocks appended
            # since (the next speaker) stay live. A superseding pump replaces
            # the live dict — then there is nothing of ours to trim.
            # ``trim`` keeps its dicts alive until here, so no block appended
            # since can have taken the address of one an eviction dropped.
            gone = {id(b) for b in trim}
            cur = _chat_streaming_state.get(chat_id)
            if (cur is not None and cur.get("live_blocks") is live_list
                    and live_list is not None):
                live_list[:] = [b for b in live_list if id(b) not in gone]

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

        # A session the pool's memory does not know has its stored binding
        # read here, for the turn's usage snapshot: a close at the turn's end
        # deletes the row on the binding writer while the cost job waits.
        from services.engines import subscription_pool
        session_id = self.session_id
        read_binding = None in subscription_pool.bound_attribution(session_id)

        def _job() -> tuple[int, dict, dict | None]:
            row = task_store.get_chat(chat_id) or {}
            return (task_store.get_last_chat_message_id(chat_id), row,
                    _stored_binding(session_id) if read_binding else None)

        def _started(f: asyncio.Future) -> None:
            if f.cancelled() or f.exception() is not None:
                return
            cutoff, row, binding = f.result()
            self._stored_binding, self._binding_read = binding, read_binding
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
        # The turn's open-turn record, its own job (a failure never touches
        # the cutoff above), by the pump that owns the chat only (a captured
        # wake's pump beside it must not replace its token). A meeting
        # pump's session is never a satellite's: none recorded.
        if _active_pumps.get(chat_id) is self:
            self._turn_token = uuid.uuid4().hex
            chat_writer.submit(chat_id, functools.partial(
                open_turns.open_turn, chat_id, self._turn_token,
                "" if self.source_type == session_kind.MEETING.source_type else session_id),
                label="turn_open")
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
            self._stored_attribution(snap)
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
        # Attribute the LLM turn to the subscription that served it, so the
        # pool can route new chats to the least-consumed account. The pool's
        # in-memory binding is read now; for a session it does not know
        # (outside the pool, or bound before a restart) the stored binding
        # the turn-start job read answers, or, when that job has not landed,
        # the cost job reads it (``_stored_attribution``).
        sub, payer = subscription_pool.bound_attribution(self.session_id)
        if self._binding_read:
            stored = self._stored_binding or {}
            if sub is None:
                sub = stored.get("subscription_id") or "default"
            if payer is None:
                payer = stored.get("user_sub") or ""
        snap["sub_id"] = sub or "default"
        snap["sub_unbound"] = sub is None
        if self.scope == "agent":
            snap["payer"] = payer
            snap["payer_unbound"] = payer is None
        return snap

    @staticmethod
    def _stored_attribution(snap: dict) -> None:
        """The cost job's half of the usage snapshot: the stored binding of a
        session the pool's memory did not know."""
        sub_unbound = snap.pop("sub_unbound", False)
        payer_unbound = snap.pop("payer_unbound", False)
        if not (sub_unbound or payer_unbound):
            return
        stored = _stored_binding(snap["session_id"]) or {}
        if sub_unbound:
            snap["sub_id"] = stored.get("subscription_id") or "default"
        if payer_unbound:
            snap["payer"] = stored.get("user_sub") or ""

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
        except psycopg.Error:
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
                except psycopg.Error as e:
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
                "ChatStreamPump chat=%s: turn rows still landing "
                "after %.0fs — database slow?", self.chat_id, waited
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
                    "ChatStreamPump chat=%s: a viewer fell "
                    "%s frames behind; resyncing it",
                    self.chat_id, _VIEWER_QUEUE_MAX)
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
            self._last_save_at = asyncio.get_running_loop().time()
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
                    await self._maybe_checkpoint()
                    continue

                # Throttled sidebar-recency touch (see _activity_touch above).
                _touch_now = time.monotonic()
                if _touch_now - self._activity_touch >= 60.0:
                    self._activity_touch = _touch_now
                    with contextlib.suppress(Exception):
                        await run_db(task_store.touch_chat, self.chat_id)

                if event.type == ERROR:
                    await self._on_error_event(event)
                    break

                if event.type == PRODUCER_DONE:
                    await self._on_producer_done()
                    break

                if event.type in (QUEUE_TURN, STEER_ACCEPTED):
                    await self._on_accept_event(event)
                    continue

                if event.type == ARTIFACT_TURN:
                    await self._on_artifact_turn(event)
                    continue

                if event.type == BG_REVIEW_TURN:
                    await self._on_review_turn(event)
                    continue

                # Process event (text, thinking, tools, metadata, etc.)
                if self._engine_done and turn_checkpoint.opens_engine_turn(event):
                    # A prompt the engine took after its result (a system
                    # prompt is sent with no pump event): a new engine turn.
                    self._engine_turn_begins()
                await self._process_event(event)
                await self._maybe_checkpoint()

        except Exception as e:
            logger.exception(
                "ChatStreamPump error: chat=%s, error=%s", self.chat_id, e,
            )
        finally:
            await self._persist_tail()
            await self._end_turn()

    async def _on_error_event(self, event: CommonEvent) -> None:
        """An ERROR event ends the turn: the typed ending, the account
        rested, the queue closed, the error frame, the ``turn_ended`` row."""
        err_msg = event.data.get("message", "")
        self.last_error = err_msg or "engine error"
        typed = turn_ending.from_dict(event.data.get("ending"))
        # Every error that ends the turn is typed: an untyped one
        # (a session gone, a producer's exception) is the
        # ``error`` ending with its own words, so the chat keeps
        # its card and the next message starts a new turn.
        ending = typed or turn_ending.TurnEnding(
            reason=turn_ending.ERROR, detail=err_msg or "engine error",
        )
        if _platform_ending and ending.reason in (turn_ending.EXITED, turn_ending.LOST):
            # The shutdown closing the session is what ended the engine.
            ending = turn_ending.TurnEnding(reason=turn_ending.RESTARTED, detail=ending.detail)
            typed = None
        self.last_ending = ending
        # Failover: a turn that died on a provider limit or overload
        # rests this session's subscription so the next chat or turn
        # picks another: a typed limit ending until the window it
        # names resets (account-wide, or for that model family only),
        # else the usual cooldown; a transient overload (529) only a
        # brief nudge, so an immediate retry isn't blocked
        # (subscription_pool.rest_after_limit).
        try:
            from services.engines import subscription_pool as _subpool
            _subpool.rest_after_limit(self.session_id, typed, err_msg)
        except Exception:  # noqa: BLE001  # resting the account spans the pool and layers: the error frame goes on
            pass
        # Closed before the viewer sees the error: the session
        # may be dead, and a message sent from here on waits in
        # the chat's queue for the next turn.
        self.close_queue()
        frame = {"pump_type": wire.PUMP_ERROR,
                 "message": err_msg if typed else ending.line(),
                 "reason": ending.reason, "resets_at": ending.resets_at}
        await self._forward(frame)
        # The ending is the chat's ``turn_ended`` row, after the
        # turn's text so far: a reload shows the card where the
        # live frame showed it. This loop BREAKS on ERROR (the
        # trailing DONE never flows through the pump), and the
        # `finally` saves the blocks in order. The flags a lost
        # partial turn needs ride the same lane, before the row's
        # save.
        self._flush_pending_text()
        self._turn_blocks.append(self._stamp_speaker(turn_ended_block(ending)))
        stamps = ending.abort_stamps()
        if stamps:
            self._submit_chat_update("abort_flags", **stamps)
        # What was queued until now goes back to its authors at
        # the pump's end; a message sent after it is the next turn.
        self._return_upto = input_queue.get(self.chat_id).seq

    async def _on_producer_done(self) -> None:
        """The producer's end: the permission queue drained, the turn's
        rest saved in order, the cost job, the title fire once."""
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
        if save_fut is None and self._checkpoint_fut is not None and not self._checkpoint_fut.done():
            save_fut = self._checkpoint_fut
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

            def _fire_title(_f=None, *, _cid=_title_cid) -> None:
                spawn(title_generator.request_chat_title(_cid),
                      name="request_chat_title", owner="stream_pump")

            if save_fut is not None:
                save_fut.add_done_callback(_fire_title)
            else:
                _fire_title()
        # all_done goes out from the finally, after this turn's rows
        # have landed: "done" means "persisted" to the queue drain
        # and the resume re-send.
        self._completed = True

    async def _on_accept_event(self, event: CommonEvent) -> None:
        """A queued turn or a steer the engine accepted: its row and its
        frame, or the typed error that ends the turn."""
        # An accept that fails (a lane or DB error) ends the turn
        # typed, with its row and its card, like any error.
        try:
            await (self._accept_queue_turn if event.type == QUEUE_TURN
                   else self._accept_steer)(event.data)
        except Exception as e:
            logger.exception("chat=%s: an accept failed", self.chat_id[:8])
            await self.event_queue.put(CommonEvent(type=ERROR, data={
                "message": f"the message could not be saved: {e}"}))

    async def _on_artifact_turn(self, event: CommonEvent) -> None:
        """Drained backchannel interactions: one event row each, forwarded
        live."""
        # Drained backchannel interactions (artifact sends AND
        # app send_prompt actions, kind-tagged): one distinct
        # event row per entry (NEVER a "user" row — the row type +
        # the framed prompt carry the provenance), forwarded live
        # so the sender's transcript shows the chip at delivery.
        from core.events import artifact_events as _ai
        self._engine_turn_begins()
        _art_rows = [
            (_ai.event_type(it), _ai.event_row_json(it))
            for it in event.data.get("interactions", [])
        ]
        _art_cid = self.chat_id

        def _art_job(_rows=_art_rows, *, _cid=_art_cid) -> None:
            for _etype, _edata in _rows:
                task_store.add_chat_message(
                    _cid, "event", "", event_type=_etype, event_data=_edata,
                )

        if _art_rows:
            chat_writer.submit(_art_cid, _art_job, label="artifact_turn")
        for it in event.data.get("interactions", []):
            frame = _ai.ws_frame(it, self.chat_id)
            frame["pump_type"] = frame.pop("type")
            await self._forward(frame)

    async def _on_review_turn(self, event: CommonEvent) -> None:
        """A run's review of its finished background work (the task
        producer's): the blocks so far saved, then one nudge row per kind on
        the lane after them, then the badges of the jobs it names cleared
        (a completion read during the settle reaches no viewer when the
        registry has no chat bound) and each kind's frame flagged ``review``
        so the viewer ends the bubble there, as the rows draw it. Never a
        person's row: nobody wrote the nudge."""
        self._flush_pending_text()
        if self._turn_blocks:
            self._save_turn_blocks()
        # The restart replay covers the engine turn starting now only.
        self._steer_saved_ranges = []
        self._engine_turn_begins()
        nudges = [n for n in event.data.get("nudges") or () if n.get("event_type")]
        if not nudges:
            return
        rows = [(n["event_type"], json.dumps({"count": n.get("count", 0),
                                               "labels": list(n.get("labels") or ())}))
                for n in nudges]
        chat_id = self.chat_id

        def _job() -> int:
            # One transaction for both kinds: never one line without the other.
            return task_store.add_chat_messages_batch(
                chat_id, [("event", "", etype, edata) for etype, edata in rows])

        def _landed(f: asyncio.Future) -> None:
            # The rows are final and no live block holds them: a reconnect
            # mid-review reads them from the history (only a person's row is
            # exempt from the in-flight cut).
            if f.cancelled() or f.exception() is not None:
                return
            self._db_msg_cutoff_id = max(self._db_msg_cutoff_id or 0, f.result() or 0)

        # The rows land before the frames, as a queued turn's do, and are
        # counted in flight the same way: a resume at the boundary drains
        # the lane before its cut, so it reads them from the history. A
        # failed write is logged by the lane; the frames still go out.
        self.accepts_in_flight += 1
        try:
            fut = chat_writer.submit(chat_id, _job, label="review_turn")
            fut.add_done_callback(_landed)
            with contextlib.suppress(Exception):
                await fut
        finally:
            self.accepts_in_flight -= 1
        live = _chat_streaming_state.get(chat_id)
        for n in nudges:
            agents = n["event_type"] == wire.PERSISTED_BG_NUDGE
            for tuid in n.get("tool_use_ids") or ():
                if not tuid:
                    continue
                if agents:
                    await self._on_subagent_end({"tool_use_id": tuid}, live)
                else:
                    # The producer knows the job ended, not how.
                    await self._on_bg_command_end({"tool_use_id": tuid}, live)
            await self._forward({"pump_type": wire.PUMP_WS_EVENT, "event": {
                "type": wire.BG_AGENTS_COMPLETE if agents else wire.BG_COMMANDS_COMPLETE,
                "count": n.get("count", 0), "review": True}})

    async def _persist_tail(self) -> None:
        """The loop's end, first half: nothing drains the queues any more,
        the producer is ended and awaited, what the turn left unflushed is
        saved (the thinking rest, the active tools, a late steer, the spoken
        leftovers) and the plan's status lands."""
        self._done = True
        # Whatever ended the pump (a cancelled producer, an exception),
        # nothing drains the queues any more.
        self.close_queue()
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
        # A steer the engine accepted after the loop's last read: its row
        # lands after the final save, and its frame still reaches the
        # attached viewers before their done.
        while not self.event_queue.empty():
            late = self.event_queue.get_nowait()
            if late.type == STEER_ACCEPTED:
                with contextlib.suppress(Exception):
                    await self._accept_steer(late.data)
        if self._leftover_messages:
            # Spoken utterances nothing sent stay in the chat as a card.
            shelve_undelivered(self.chat_id, self._leftover_messages)
            self._leftover_messages = []

        if self.implementing_plan:
            chat_writer.submit(
                self.chat_id,
                functools.partial(
                    task_store.update_chat_plan_status,
                    self.chat_id, self.implementing_plan, "implemented",
                ),
                label="plan_status",
            )


    async def _end_turn(self) -> None:
        """The loop's end, second half: the turn-end job (this pump's only
        when it still owns the chat), the rows drained before all_done, the
        live state left or trimmed to the running badges, the registry, the
        queue's hand-off and the stored wakes' replay (not for a chat being
        deleted), pump_ended, the sidebar dot and the alert, the library
        reconcile."""
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
        if self._turn_token and not self._keeps_open_turn():
            chat_writer.submit(self.chat_id, functools.partial(
                open_turns.end_turn, self.chat_id, self._turn_token), label="turn_close")
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
                # What the chat's queue still holds: what was queued
                # before a failure goes back to its authors, the rest
                # goes out as the next turn (an attached viewer drains its
                # own on its loop's way out first). A pump that ended
                # without its result and without a Stop failed too. A
                # superseded pump leaves it to the pump that owns the
                # chat now.
                if (self._return_upto is None and not self._completed
                        and not self.stopped):
                    self._return_upto = input_queue.get(self.chat_id).seq
                # A deleted chat's queue goes with the chat: nothing is
                # returned or delivered, no wake is replayed.
                if not self.deleting:
                    input_queue.on_pump_end(
                        self.chat_id, return_upto=self._return_upto,
                        viewers=self.has_viewers,
                    )
                    if self.chat_id in _stored_wake_chats:
                        _stored_wake_chats.discard(self.chat_id)
                        _spawn_replay(self.chat_id)

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
                    "chat=%s: superseded pump — skipping ready "
                    "broadcast + end-of-turn notification", self.chat_id
                )
            elif get_subagent_registry(self.session_id).has_pending:
                logger.debug(
                    "chat=%s: end-of-turn notification deferred — "
                    "background subagents still running (nudge turn will fire it)",
                    self.chat_id
                )
            else:
                self._fire_end_of_turn(end_row, end_meeting, end_label)
        except Exception:  # noqa: BLE001  # the end-of-turn fan-out must not break the pump's teardown
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
        except ImportError:
            pass

        logger.info(
            "ChatStreamPump ended: chat=%s, "
            "blocks=%s, plan=%s",
            self.chat_id, len(self._turn_blocks), self.implementing_plan or 'none'
        )


    def _keeps_open_turn(self) -> bool:
        """The turn's open-turn record outlives the pump: the replay writes
        this turn (it takes the record), or the platform is ending and the
        turn got neither its engine's result nor a typed ending, and nobody
        stopped or deleted it (the boot writes its closing row)."""
        if self.chat_id in _recovery_suppress_flush:
            return True
        return (_platform_ending and not self._engine_done and self.last_ending is None
                and not self.stopped and not self.deleting)

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
        # A deleted chat's turn ended because the chat is going: the dot
        # clears everywhere, nothing "finished".
        if self.source_type == session_kind.TASK.source_type or self.deleting:
            return
        if not in_meeting:
            spawn(notification_manager.fire_ephemeral(
                chat["user_sub"],
                title=f"{agent_label or chat.get('agent') or ''} finished",
                body="Response ready",
                chat_id=self.chat_id,
            ), name="fire_ephemeral", owner="stream_pump")

    def _live_append_text(self, live: dict, text: str, note: bool | None = None):
        """Append text to the last text block in live_blocks when it is of the
        same kind (a progress note or not; ``None``: either), or create a new
        one (also instead of one a checkpoint in flight will trim)."""
        blocks = live["live_blocks"]
        last = blocks[-1] if blocks else None
        if (last is not None and last.get("type") == wire.LIVE_TEXT and id(last) not in self._checkpoint_trims
                and (note is None or bool(last.get("note")) == note)):
            last["content"] += text
        else:
            blocks.append({"type": wire.TEXT, "content": text, **({"note": True} if note else {})})

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

    # The two handlers kept beside the globals they read while the rest
    # live in pump_event_handlers.py: this one's coalescing window and clock
    # share a home with _forward_delta's (the clock the characterization
    # trace replaces is this module's `time`).
    async def _on_workflow_progress(self, ed: dict, live: dict | None) -> None:
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

    # Kept here: tests/core/test_status_surface.py pins the `completed`
    # compare below to this file's path.
    async def _on_context_compact(self, ed: dict, live: dict | None) -> None:
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
            # Every artifact kind maps to a frame (artifact_events.KINDS).
            evt = cast("dict", artifact_event_from_perm_item(perm_data))
            if artifact.block:
                evt = self._stamp_speaker(evt)
                self._flush_pending_text()
            if artifact.evicts:
                _evict_latest(self._turn_blocks, artifact.evicts)
                if live:
                    _evict_latest(live["live_blocks"], artifact.evicts)
            if artifact.deferred:
                self._buffer_preview(evt, cast("str", artifact.identity), live)
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
        elif evt_type == wire.ITEM_PROMPT_RETIRED:
            await self._retire_prompt(perm_data.get("request_id", ""),
                                      caller_gone=bool(perm_data.get("caller_gone")))
        elif evt_type == wire.ITEM_MODE_RESTORED:
            mode = perm_data.get("mode") or ASK_MODE
            self._submit_chat_update("mode_restored", permission_mode=mode)
            await self._forward({"pump_type": wire.PUMP_MODE_RESTORED, "mode": mode})


# The background-work monitors live in pump_bg_monitors.py; re-exported here
# so `from core.events.stream_pump import bg_monitor_running` (ws/dashboard) works.
from core.events.pump_bg_monitors import (  # noqa: F401
    _bg_agent_monitor,
    _bg_command_monitor,
    bg_command_monitor_running,
    bg_monitor_running,
)
