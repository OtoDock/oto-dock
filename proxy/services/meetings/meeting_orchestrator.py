"""Multi-agent meeting orchestrator — directed message routing with parallel execution.

Manages the lifecycle of agent meetings. Instead of round-robin, agents use
`direct_to` to control who speaks next. Multiple agents addressed simultaneously
run in parallel. All responses go to a shared transcript visible to everyone.
"""

import asyncio
import contextlib
import json
import logging
from core.events import input_queue, tool_roles
from core import placement
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone

from storage import database as task_store
from storage.agents import agent_store
from storage.chat import meeting_status
from storage.pg import run_db
from core.session.session_manager import get_execution_layer
from core.events.common_events import (
    CommonEvent, TEXT, THINKING, PRODUCER_DONE,
    SYSTEM, QUEUE_TURN, TOOL_USE, TOOL_INPUT, TOOL_RESULT, METADATA,
    PLAN_MODE,
)
from core.events.stream_pump import ChatStreamPump, _active_pumps
from core.events.bg_command_state import get_bg_command_registry
from core.execution_layer import DEFAULT_EXECUTION_PATH
from core.events.pump_bg_monitors import (
    _bg_agent_monitor, bg_monitor_running,
    _bg_command_monitor, bg_command_monitor_running,
)
from core.session.session_state import (
    _sessions, _save_sessions,
    get_permission_queue, get_subagent_registry, set_meeting_session_info,
    cleanup_meeting_session_info, cleanup_session_permission_state,
    clear_meeting_turn_routed, note_meeting_turn_text,
)
# Config + per-turn prompt builders live in services.meetings.meeting_context; imported
# here so existing call sites and tests (meeting_orchestrator.build_meeting_agent_config
# / build_turn_prompt / _parse_directed_agents) are unchanged.
from services.meetings.meeting_context import (
    _parse_directed_agents,
    build_meeting_agent_config,
    build_turn_prompt,
)
from core.session import session_kind
from auth import roles
from ws import wire_events as wire

logger = logging.getLogger("claude-proxy")

_active_meetings: dict[str, asyncio.Task] = {}


async def shutdown_meetings() -> None:
    """Cancel all active meeting orchestration tasks for graceful shutdown."""
    if not _active_meetings:
        return

    count = len(_active_meetings)
    logger.info(f"Meeting shutdown: cancelling {count} active meeting(s)")
    for meeting_id, task in list(_active_meetings.items()):
        if not task.done():
            task.cancel()

    tasks = [t for t in _active_meetings.values() if not t.done()]
    if tasks:
        done, pending = await asyncio.wait(tasks, timeout=15)
        if pending:
            logger.warning(
                f"Meeting shutdown: {len(pending)} meeting(s) didn't finish in 15s"
            )

    logger.info("Meeting shutdown: all active meetings cancelled")


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# ---------------------------------------------------------------------------
# Turn result dataclass
# ---------------------------------------------------------------------------

@dataclass
class TurnResult:
    agent: str
    events: list  # collected CommonEvent list
    content: str = ""
    thinking: str = ""
    tools: list = field(default_factory=list)
    cost: float = 0.0
    cost_is_delta: bool = False  # True if cost is per-turn (Codex/Direct), False if cumulative (CLI)
    directed_to: list[str] | None = None  # from direct_to tool, None = broadcast
    tools_called: set = field(default_factory=set)  # end_meeting, propose_conclude, leave_meeting
    tail_text: int = 0  # TEXT chars after the last data-tool result (thin-turn restate signal)


# Thin-turn auto-restate: only an agent's response TEXT is relayed to the
# other participants — adaptive-effort models sometimes leave their findings
# in (non-streamed) thinking and route onward believing the report was
# delivered, so the others receive nothing and the meeting burns a manual
# restatement round. Two signatures trigger a restate: a turn with NO
# response text at all (nothing to relay, whatever tools ran — observed live
# with only ToolSearch in the turn), and a tool-using turn where no text
# followed the last data-tool result. `_THIN_TURN_MAX_CHARS` guards the
# second shape (a full report written BEFORE a final verification tool must
# not trigger a restate).
_THIN_TURN_MAX_CHARS = 300


def _tool_resets_tail(name: str) -> bool:
    """Whether a TOOL_RESULT for `name` resets the tail-text tracker. Meeting
    tools only route (their result lands after the written report) and a
    tool-discovery call only loads schemas (models legitimately run it
    between the report text and direct_to) — neither invalidates text
    written before it."""
    return "meetings-mcp__" not in name and tool_roles.role_of(name) != tool_roles.DISCOVERY


class _TurnTracker:
    """Per-turn bookkeeping shared by both turn runners.

    Collects the relayable text, thinking and tool names, the routing signal
    (``direct_to`` targets, ``end_meeting`` & co), the METADATA cost, and
    decides which events reach the pump:

    - METADATA and PLAN_MODE never do (cost is tracked per turn here; plan
      mode is disabled in meetings).
    - TEXT after a meeting tool's close event is dropped — the transcript
      echo guard — EXCEPT after the MODERATOR's ``end_meeting`` when fewer
      than ``_THIN_TURN_MAX_CHARS`` were written before it: that text IS the
      summary (observed live 2026-09-09: a summary written after the call
      was discarded and the meeting concluded with a one-line opener). The
      close event of a tool is emitted at the next content block, so the
      salvage decision lands right before the summary's first delta.
    - The text length is mirrored into the meeting session info
      (``note_meeting_turn_text``) so the hook's turn-end backstop
      (api/hooks) can word its end_meeting reason from the same number.
    """

    def __init__(self, agent_slug: str, session_id: str, is_moderator: bool):
        self.agent = agent_slug
        self.session_id = session_id
        self.is_moderator = is_moderator
        self.text: list[str] = []
        self.thinking: list[str] = []
        self.tools: list[dict] = []
        self.cost = 0.0
        self.cost_is_delta = False
        self.directed_to: list[str] | None = None
        self.tools_called: set[str] = set()
        self.tail_text = 0
        self.chars = 0
        self.meeting_tool_done = False
        self.salvage = False

    def observe(self, event: CommonEvent) -> bool:
        """Account for one stream event; True when it should be forwarded."""
        if event.type == METADATA:
            # Cost only — meeting costs are tracked separately via
            # meeting_turns, never forwarded to the pump.
            cost = event.data.get("cost_usd", 0)
            if cost > 0:
                self.cost = cost
                self.cost_is_delta = bool(event.data.get("cost_is_delta"))
            return False
        if event.type == PLAN_MODE:
            return False
        if event.type == TEXT:
            if self.meeting_tool_done and not self.salvage:
                return False
            content = event.data.get("content", "")
            self.text.append(content)
            self.tail_text += len(content)
            self.chars += len(content)
            note_meeting_turn_text(self.session_id, self.chars)
        elif event.type == THINKING:
            txt = event.data.get("text", "") or event.data.get("content", "")
            if txt:
                self.thinking.append(txt)
        elif event.type == TOOL_USE:
            self.tools.append({"name": event.data.get("name", "")})
        elif event.type == TOOL_INPUT:
            tool_name = event.data.get("name", "")
            if "meetings-mcp__direct_to" in tool_name:
                ti = event.data.get("tool_input", {})
                parsed = _parse_directed_agents(ti.get("agents"))
                if parsed is not None:
                    self.directed_to = parsed  # last usable direct_to wins
            elif "meetings-mcp__end_meeting" in tool_name:
                self.tools_called.add("end_meeting")
            elif "meetings-mcp__propose_conclude" in tool_name:
                self.tools_called.add("propose_conclude")
            elif "meetings-mcp__leave_meeting" in tool_name:
                self.tools_called.add("leave_meeting")
        elif event.type == TOOL_RESULT:
            # name FIRST: the stream's tool_id is a UUID, so matching it
            # first left the suppression permanently off (transcript-echo
            # junk after direct_to was displayed AND relayed).
            name = event.data.get("name", "") or event.data.get("tool_id", "")
            if "meetings-mcp__" in name:
                if (self.is_moderator and name.endswith("end_meeting")
                        and self.chars < _THIN_TURN_MAX_CHARS):
                    self.salvage = True
                self.meeting_tool_done = True
            elif _tool_resets_tail(name):
                self.tail_text = 0
        return True

    def result(self) -> TurnResult:
        return TurnResult(
            agent=self.agent,
            events=[],  # already forwarded live
            content="".join(self.text),
            thinking="".join(self.thinking),
            tools=self.tools,
            cost=self.cost,
            cost_is_delta=self.cost_is_delta,
            directed_to=self.directed_to,
            tools_called=self.tools_called,
            tail_text=self.tail_text,
        )


# Per-meeting-session execution layer, captured at session-create so every
# turn and close reuses the SAME layer the session was started on. A bare
# get_execution_layer at those sites would re-resolve WITHOUT the meeting
# creator's user_sub/role and, for a user-override meeting, return the wrong
# layer (sending/closing on local while the session lives on the user's box).
_meeting_session_layers: dict[str, object] = {}


def _start_participant_bg_monitors(
    agent_slug: str, session_id: str, chat_id: str,
) -> None:
    """Post-turn background-work watch for a meeting participant.

    Dashboard chats launch these from the WS turn loop; meeting participants
    have no WS, so the orchestrator is the launch point. Meeting agents are
    prompt-instructed not to background work, but that is prompt-only — when
    one does anyway, its completions must still resolve against the meeting's
    parent chat (badge clears + review nudge) instead of leaking as
    forever-pending registry entries. The CLI layer resets both registries at
    every turn start, so the parent-chat binding is re-stamped after each
    turn, not just at session create.
    """
    reg = get_subagent_registry(session_id)
    bgreg = get_bg_command_registry(session_id)
    reg.chat_id = chat_id
    bgreg.chat_id = chat_id
    layer = _meeting_session_layers.get(session_id) or get_execution_layer(agent_slug)
    if reg.has_pending and not bg_monitor_running(session_id):
        asyncio.create_task(
            _bg_agent_monitor(layer, session_id, chat_id, reg.pending_count)
        )
    if bgreg.has_pending and not bg_command_monitor_running(session_id):
        asyncio.create_task(
            _bg_command_monitor(layer, session_id, chat_id, bgreg.pending_count)
        )


def _drain_system_notes(
    pump: ChatStreamPump, transcript: list[dict],
    pending: dict[str, list], moderator: str,
) -> None:
    """Deliver ``pump.system_queue`` entries to the moderator as system notes.

    The bg monitors' review nudges arrive via ``queue_pump_prompt(system=True)``
    → ``pump.system_queue`` — meetings have no WS turn loop to drain it, so
    without this the nudges would sit unread until meeting end. The note goes
    on the transcript (all agents see it) and queues the moderator, who
    decides which participant reviews the finished work.
    """
    while pump.system_queue:
        note = pump.system_queue.pop(0)
        entry = {"agent": "system", "content": note,
                 "thinking": "", "tools": [], "role": "system"}
        transcript.append(entry)
        pending.setdefault(moderator, []).append(entry)


def _departed(active: list[str], meeting: dict | None) -> list[str]:
    """The agents of the round loop's list that the row's
    ``active_participants`` no longer holds; none when the row or its list
    cannot be read; the loop never takes an agent back from the row."""
    if not meeting:
        return []
    try:
        row_active = json.loads(meeting.get("active_participants") or "")
    except (TypeError, ValueError):
        return []
    if not isinstance(row_active, list):
        return []
    return [a for a in active if a not in row_active]


async def _announce_departure(
    agent_slug: str, agent_sessions: dict[str, str], event_queue: asyncio.Queue,
) -> None:
    """A participant is out of the meeting: the dashboard greys it out, its
    hook route is dropped and its session closes now (the meeting's end
    closes every session again, which a closed one tolerates)."""
    ad = await run_db(agent_store.get_agent, agent_slug)
    await event_queue.put(CommonEvent(type=SYSTEM, data={
        "subtype": wire.SUBTYPE_MEETING_AGENT_LEFT,
        "agent": agent_slug,
        "agent_display_name": (ad or {}).get("display_name", agent_slug),
        "agent_color": (ad or {}).get("color", ""),
    }))
    sid = agent_sessions.get(agent_slug, "")
    cleanup_meeting_session_info(sid)
    if sid:
        with contextlib.suppress(Exception):
            layer = _meeting_session_layers.get(sid) or get_execution_layer(agent_slug)
            await layer.close_session(sid)


# ---------------------------------------------------------------------------
# Live turn runner (single agent — events forwarded directly to pump)
# ---------------------------------------------------------------------------

async def _run_live_turn(
    agent_slug: str,
    agent_sessions: dict[str, str],
    meeting: dict,
    transcript: list[dict],
    pending: dict[str, list],
    event_queue: asyncio.Queue,
    meeting_id: str,
) -> TurnResult:
    """Run a single agent's turn with live streaming to the pump."""
    # Build prompt
    pending_msgs = pending.get(agent_slug, [])
    prompt_type = "normal"
    propose_from = None
    for msg in pending_msgs:
        if isinstance(msg, dict) and msg.get("type") in ("start", "wrapup", "checkin", "conclude_proposal", "restate"):
            prompt_type = msg["type"]
            propose_from = msg.get("from")
            break

    prompt = build_turn_prompt(meeting, agent_slug, transcript,
                               prompt_type=prompt_type, propose_from=propose_from)

    # Emit turn_start
    ad = await run_db(agent_store.get_agent, agent_slug)
    await event_queue.put(CommonEvent(type=SYSTEM, data={
        "subtype": wire.SUBTYPE_MEETING_TURN_START,
        "meeting_id": meeting_id,
        "agent": agent_slug,
        "agent_display_name": (ad or {}).get("display_name", agent_slug),
        "agent_color": (ad or {}).get("color", ""),
    }))

    # Stream events live to pump — reuse the layer the session was started on.
    session_id = agent_sessions[agent_slug]
    layer = _meeting_session_layers.get(session_id) or get_execution_layer(agent_slug)
    tracker = _TurnTracker(agent_slug, session_id,
                           agent_slug == meeting["moderator"])

    try:
        # The hook backstop's per-turn state spans exactly this send: cleared
        # before (a restate may run tools again) and after (a flag left set
        # would deny the between-turn hooks of tolerated background work).
        clear_meeting_turn_routed(session_id)
        async with layer.session_lock(session_id):
            async for event in layer.send_message(session_id, prompt):
                if not tracker.observe(event):
                    continue
                # Tag with meeting agent and forward LIVE to pump
                event.data["_meeting_agent"] = agent_slug
                await event_queue.put(event)

    except Exception as e:
        logger.error(f"Meeting {meeting_id}: agent {agent_slug} failed: {e}")
        await event_queue.put(CommonEvent(type=SYSTEM, data={
            "subtype": wire.SUBTYPE_MEETING_AGENT_FAILED,
            "agent": agent_slug,
            "error": str(e)[:200],
        }))
        return TurnResult(agent=agent_slug, events=[], tools_called={"_failed"})
    finally:
        clear_meeting_turn_routed(session_id)

    # Background work the agent left running keeps resolving after its turn.
    _start_participant_bg_monitors(agent_slug, session_id, meeting["parent_chat_id"])

    # Emit turn_end — cost is added later by meeting_produce after computing
    # per-turn delta (METADATA cost is cumulative per CLI session).
    await event_queue.put(CommonEvent(type=SYSTEM, data={
        "subtype": wire.SUBTYPE_MEETING_TURN_END,
        "agent": agent_slug,
        "cost_usd": 0,  # placeholder — meeting_produce emits actual cost as metadata
    }))

    return tracker.result()


# ---------------------------------------------------------------------------
# Parallel batch runner
# ---------------------------------------------------------------------------

async def _run_parallel_batch(
    ready_agents: list[str],
    agent_sessions: dict[str, str],
    meeting: dict,
    transcript: list[dict],
    pending: dict[str, list],
    event_queue: asyncio.Queue,
    meeting_id: str,
) -> list[TurnResult]:
    """Run multiple agents in parallel with live streaming.

    All LLM calls start simultaneously. Each agent's events go to a
    per-agent queue. The coordinator streams the first agent to produce
    events live to the pump. When it finishes, the next agent's queued
    events flush immediately (they were generating in parallel).
    """

    # Build prompts
    prompts: dict[str, str] = {}
    for agent_slug in ready_agents:
        pending_msgs = pending.get(agent_slug, [])
        prompt_type = "normal"
        propose_from = None
        for msg in pending_msgs:
            if isinstance(msg, dict):
                t = msg.get("type", "")
                if t in ("start", "wrapup", "checkin", "conclude_proposal", "restate"):
                    prompt_type = t
                    propose_from = msg.get("from")
                    break
        prompts[agent_slug] = build_turn_prompt(
            meeting, agent_slug, transcript,
            prompt_type=prompt_type, propose_from=propose_from,
        )

    # Per-agent event queues and result holders
    agent_queues: dict[str, asyncio.Queue] = {a: asyncio.Queue() for a in ready_agents}
    agent_results: dict[str, TurnResult] = {}

    async def _run_agent(agent_slug: str) -> None:
        """Run one agent, forwarding events to its queue."""
        q = agent_queues[agent_slug]
        sid = agent_sessions[agent_slug]
        layer = _meeting_session_layers.get(sid) or get_execution_layer(agent_slug)
        tracker = _TurnTracker(agent_slug, sid, agent_slug == meeting["moderator"])

        try:
            clear_meeting_turn_routed(sid)
            async with layer.session_lock(sid):
                async for event in layer.send_message(sid, prompts[agent_slug]):
                    if not tracker.observe(event):
                        continue
                        await q.put(event)
        except Exception as e:
            logger.error(f"Meeting {meeting_id}: agent {agent_slug} failed: {e}")
            tracker.tools_called.add("_failed")
        finally:
            clear_meeting_turn_routed(sid)

        if "_failed" not in tracker.tools_called:
            # Background work the agent left running keeps resolving after its turn.
            _start_participant_bg_monitors(agent_slug, sid, meeting["parent_chat_id"])

        await q.put(None)  # done sentinel
        agent_results[agent_slug] = tracker.result()

    # Start all agents simultaneously
    tasks = [asyncio.create_task(_run_agent(a)) for a in ready_agents]

    # Stream agents one at a time — first to produce events goes first
    remaining = set(ready_agents)
    results_ordered: list[TurnResult] = []

    while remaining:
        # Wait for any remaining agent to have events ready
        chosen = None
        while not chosen:
            for a in list(remaining):
                if not agent_queues[a].empty():
                    chosen = a
                    break
            if not chosen:
                await asyncio.sleep(0.05)

        remaining.discard(chosen)

        result_ref = agent_results.get(chosen)
        if result_ref and "_failed" in result_ref.tools_called:
            # Agent already failed before producing events
            await event_queue.put(CommonEvent(type=SYSTEM, data={
                "subtype": wire.SUBTYPE_MEETING_AGENT_FAILED,
                "agent": chosen,
                "error": (result_ref.content or "unknown error")[:200],
            }))
            # Drain sentinel
            while True:
                ev = await agent_queues[chosen].get()
                if ev is None:
                    break
            results_ordered.append(result_ref)
            continue

        # Emit turn_start
        ad = await run_db(agent_store.get_agent, chosen)
        await event_queue.put(CommonEvent(type=SYSTEM, data={
            "subtype": wire.SUBTYPE_MEETING_TURN_START,
            "meeting_id": meeting_id,
            "agent": chosen,
            "agent_display_name": (ad or {}).get("display_name", chosen),
            "agent_color": (ad or {}).get("color", ""),
        }))

        # Forward events live from this agent's queue
        while True:
            event = await agent_queues[chosen].get()
            if event is None:
                break  # agent done
            event.data["_meeting_agent"] = chosen
            await event_queue.put(event)

        # Collect result and emit turn_end — cost is added by meeting_produce
        result = agent_results.get(chosen)
        await event_queue.put(CommonEvent(type=SYSTEM, data={
            "subtype": wire.SUBTYPE_MEETING_TURN_END,
            "agent": chosen,
            "cost_usd": 0,  # placeholder — meeting_produce emits actual cost
        }))
        if result:
            results_ordered.append(result)

    # Ensure all tasks complete
    await asyncio.gather(*tasks, return_exceptions=True)

    return results_ordered


# ---------------------------------------------------------------------------
# Meeting producer (directed queue architecture)
# ---------------------------------------------------------------------------

async def meeting_produce(
    meeting_id: str,
    agent_sessions: dict[str, str],
    event_queue: asyncio.Queue,
    pump: ChatStreamPump,
    meeting_participants: list[dict] | None = None,
) -> None:
    """Producer for the meeting — directed message routing with parallel execution."""
    meeting = await run_db(task_store.get_meeting, meeting_id)
    if not meeting:
        return

    max_turns = meeting.get("max_turns", 30)
    moderator = meeting["moderator"]
    active_participants = json.loads(meeting["active_participants"])

    # Emit meeting_started as the very first event (so WS receives it live)
    await event_queue.put(CommonEvent(type=SYSTEM, data={
        "subtype": wire.SUBTYPE_MEETING_STARTED,
        "meeting_id": meeting_id,
        "topic": meeting["topic"],
        "participants": meeting_participants or [],
        "moderator": moderator,
        "max_turns": max_turns,
    }))
    transcript: list[dict] = []
    total_turns = 0
    meeting_total_cost = 0.0
    # Per-agent cumulative cost tracking — METADATA from CLI is cumulative,
    # so we compute per-turn delta: turn_cost = metadata_cost - last_cost.
    _agent_cumulative_cost: dict[str, float] = {}
    last_speaker: str | None = None
    turns_since_moderator = 0
    paused_pending: dict | None = None
    meeting_active = True
    # Thin-turn auto-restate state: one restate per agent per meeting
    # (loop-proof), and the thin turn's routing deferred until the restated
    # message exists so the recipient isn't queued to read an empty report.
    restated_agents: set[str] = set()
    pending_restates: dict[str, list[str] | None] = {}

    # Per-agent pending queues
    pending: dict[str, list] = {a: [] for a in active_participants}

    try:
        # Moderator speaks first. Inside the try: a moderator that is not a
        # participant ends the meeting with an error and PRODUCER_DONE still
        # goes out, so the host chat's pump ends.
        pending[moderator].append({"type": "start"})
        while meeting_active and total_turns < max_turns:
            # Find agents with pending messages
            ready = [a for a in active_participants if pending.get(a)]

            # A pending restate runs ALONE. The thin turn's routing is
            # deferred, but another agent's routing from the same round can
            # still queue a recipient — running it in parallel with the
            # restate lets it read the transcript without the restated
            # findings (observed live: the moderator ended the meeting while
            # the restate was still streaming, and the batch's end_meeting
            # break discarded the restated content entirely).
            if pending_restates:
                restate_ready = [a for a in ready if a in pending_restates]
                if restate_ready:
                    ready = restate_ready

            if not ready:
                if last_speaker == moderator or moderator not in active_participants:
                    # Moderator was last (natural conclusion), or is out of
                    # the meeting: a wrap-up queued for it would never run
                    # and the loop would go round without yielding.
                    break
                else:
                    # Auto-queue moderator for wrap-up
                    pending.setdefault(moderator, []).append({"type": "wrapup"})
                    continue

            # Snapshot pending for ready agents, then clear
            pending_snapshot = {a: list(pending.get(a, [])) for a in ready}
            for a in ready:
                pending[a] = []

            if len(ready) == 1:
                # Single agent: stream live to pump (no buffering)
                batch_results = [await _run_live_turn(
                    ready[0], agent_sessions, meeting, transcript,
                    pending_snapshot, event_queue, meeting_id,
                )]
            else:
                # Multiple agents: parallel execution, sequential display
                batch_results = await _run_parallel_batch(
                    ready, agent_sessions, meeting, transcript,
                    pending_snapshot, event_queue, meeting_id,
                )

            # Process results: update transcript, route responses
            batch_agents = set(r.agent for r in batch_results)

            for result in batch_results:
                if "_failed" in result.tools_called:
                    active_participants = [a for a in active_participants if a != result.agent]
                    pending_restates.pop(result.agent, None)
                    # Under the row's lock: a leave written by the route
                    # meanwhile stays written.
                    await run_db(task_store.remove_active_participant, meeting_id,
                                 result.agent, meeting_status.LEAVABLE)
                    try:
                        _sid = agent_sessions[result.agent]
                        layer = _meeting_session_layers.get(_sid) or get_execution_layer(result.agent)
                        await layer.close_session(_sid)
                    except Exception:
                        pass
                    # A failed agent must not strand the meeting: if the
                    # MODERATOR failed there is nobody to drive rounds or
                    # conclude — end now (the meeting previously froze with no
                    # further turns until a proxy restart marked it orphaned).
                    # If too few participants remain, wrap up via the
                    # moderator (mirrors the leave_meeting path).
                    if result.agent == moderator:
                        meeting_active = False
                    elif len(active_participants) < 2:
                        pending.setdefault(moderator, []).append({"type": "wrapup"})
                    continue

                total_turns += 1
                last_speaker = result.agent

                # Compute per-turn cost delta.
                # CLI METADATA is cumulative per session → compute delta.
                # Codex/Direct METADATA has cost_is_delta=True → use directly.
                raw_cost = result.cost or 0
                if result.cost_is_delta:
                    # Cost is already per-turn (Codex, Direct LLM)
                    turn_delta = raw_cost
                else:
                    # Cost is cumulative (CLI) — compute delta per agent session
                    prev_cost = _agent_cumulative_cost.get(result.agent, 0)
                    turn_delta = max(0, raw_cost - prev_cost)
                    if raw_cost > 0:
                        _agent_cumulative_cost[result.agent] = raw_cost
                meeting_total_cost += turn_delta

                # Emit cost as metadata so the pump can forward to frontend.
                # The turn_end event has cost=0; this separate event carries the delta.
                if turn_delta > 0:
                    await event_queue.put(CommonEvent(type=METADATA, data={
                        "cost_usd": turn_delta,
                        "_meeting_cost": True,  # flag so pump knows this is meeting cost
                        # The speaker's session: the pump resolves cost_billed
                        # (show/hide) per participant account, not the host's.
                        "session_id": agent_sessions.get(result.agent, ""),
                    }))

                # Add to transcript
                entry = {
                    "agent": result.agent,
                    "content": result.content,
                    "thinking": result.thinking,
                    "tools": result.tools,
                    "role": "assistant",
                }
                transcript.append(entry)

                # Save to DB
                await run_db(
                    task_store.add_meeting_turn,
                    meeting_id, total_turns, 0, result.agent, "assistant",
                    result.content, result.thinking,
                    json.dumps(result.tools),
                    agent_sessions.get(result.agent, ""),
                    turn_delta,
                )

                # Check end_meeting — the MODERATOR's only. The signal is
                # recorded at TOOL_INPUT, before the API refuses a
                # participant's call (403), so without this guard any
                # participant could end the meeting by calling it.
                if "end_meeting" in result.tools_called and result.agent == moderator:
                    meeting_active = False
                    break

                # Check propose_conclude — pause, queue only moderator
                if "propose_conclude" in result.tools_called:
                    paused_pending = {a: list(pending.get(a, [])) for a in pending}
                    for a in pending:
                        pending[a] = []
                    pending.setdefault(moderator, []).append({
                        "type": "conclude_proposal",
                        "from": result.agent,
                    })
                    break

                # Check leave_meeting
                if "leave_meeting" in result.tools_called:
                    active_participants = [a for a in active_participants if a != result.agent]
                    pending_restates.pop(result.agent, None)
                    pending.pop(result.agent, None)
                    # The route's own write took it out already when the
                    # call succeeded; under the row's lock either way, so a
                    # concurrent leave stays written.
                    await run_db(task_store.remove_active_participant, meeting_id,
                                 result.agent, meeting_status.LEAVABLE)
                    await _announce_departure(result.agent, agent_sessions, event_queue)
                    if result.agent == moderator:
                        # Nobody is left to drive the rounds or conclude:
                        # the meeting ends, as when the moderator fails.
                        meeting_active = False
                    elif len(active_participants) < 2:
                        if last_speaker != moderator:
                            pending.setdefault(moderator, []).append({"type": "wrapup"})
                        else:
                            meeting_active = False
                    continue

                # Thin-turn auto-restate: data tools ran but no text followed
                # the last data-tool result — the findings stayed in
                # (non-streamed) thinking and the relay would deliver nothing
                # of substance. Queue the SAME agent to restate before the
                # recipients run; the thin entry stays on the transcript
                # (routing controls turn order, not visibility).
                directed = result.directed_to
                if result.agent in pending_restates:
                    # This turn IS the restate — deliver it, inheriting the
                    # deferred targets when it didn't call direct_to again.
                    deferred = pending_restates.pop(result.agent)
                    if directed is None:
                        directed = deferred
                else:
                    ran_data_tools = any(
                        t.get("name") and _tool_resets_tail(t["name"])
                        for t in result.tools
                    )
                    relayable = len(result.content.strip())
                    if (result.agent not in restated_agents
                            and (relayable == 0
                                 or (ran_data_tools and result.tail_text == 0
                                     and relayable < _THIN_TURN_MAX_CHARS))):
                        restated_agents.add(result.agent)
                        pending_restates[result.agent] = directed
                        pending.setdefault(result.agent, []).append({"type": "restate"})
                        logger.info(
                            f"Meeting {meeting_id}: thin turn from {result.agent} "
                            f"({len(result.content)} chars, no text after tools) — restating"
                        )
                        continue

                # Route response based on direct_to
                if directed is not None:
                    for target in directed:
                        if target in active_participants and target != result.agent:
                            pending.setdefault(target, []).append(entry)
                else:
                    # Broadcast to all except self and agents in same batch
                    for other in active_participants:
                        if other != result.agent and other not in batch_agents:
                            pending.setdefault(other, []).append(entry)

            # Check if moderator decided to resume after propose_conclude, or
            # whether an end or a leave landed from outside this loop.
            meeting = await run_db(task_store.get_meeting, meeting_id)
            # A leave through the route (the creator, an admin, the master
            # key, or the agent's own call) edits only the row: the agents it
            # took out leave the loop here, after the round they may have
            # spoken in, and the work queued for them is dropped.
            gone = _departed(active_participants, meeting)
            if gone:
                active_participants = [a for a in active_participants if a not in gone]
                for agent in gone:
                    pending.pop(agent, None)
                    pending_restates.pop(agent, None)
                    if paused_pending:
                        paused_pending.pop(agent, None)
                    await _announce_departure(agent, agent_sessions, event_queue)
                if moderator in gone:
                    meeting_active = False
                elif len(active_participants) < 2 and meeting_active:
                    if last_speaker != moderator:
                        pending.setdefault(moderator, []).append({"type": "wrapup"})
                    else:
                        meeting_active = False
            if meeting and meeting["status"] == meeting_status.PAUSED:
                # The moderator spoke without ending: resume. One conditional
                # write, so an end that lands between the read and here
                # (CONCLUDING) is never overwritten back to ACTIVE.
                if moderator in batch_agents and meeting_active and await run_db(
                        task_store.update_meeting_if, meeting_id,
                        (meeting_status.PAUSED,), status=meeting_status.ACTIVE):
                    if paused_pending:
                        for a, msgs in paused_pending.items():
                            if a != moderator and msgs and a in active_participants:
                                pending.setdefault(a, []).extend(msgs)
                        paused_pending = None
            elif meeting and meeting["status"] in meeting_status.ROUND_STOPS:
                meeting_active = False

            # Auto-queue moderator after 3+ turns without speaking
            if moderator in batch_agents:
                turns_since_moderator = 0
            else:
                turns_since_moderator += len([r for r in batch_results if "_failed" not in r.tools_called])

            # Hold the check-in while a restate is in flight — a moderator
            # turn queued now would run in parallel with the restating agent
            # and read the transcript before the restated findings exist.
            if (turns_since_moderator >= 3 and not pending.get(moderator)
                    and not pending_restates):
                pending.setdefault(moderator, []).append({"type": "checkin"})
                turns_since_moderator = 0

            # Check for user messages: the chat's queue, read per round (a
            # meeting turn never takes a steer). The pump writes their rows,
            # after the round's content, and clears the chips.
            queued_now = input_queue.get(pump.chat_id).take()
            if queued_now:
                await event_queue.put(CommonEvent(type=QUEUE_TURN, data={"inputs": queued_now}))
            for queued in queued_now:
                # The agents read the engine text (attachment paths included);
                # the row keeps the raw text and the attachment meta.
                user_entry = {
                    "agent": "user",
                    "content": queued.item.cli_text,
                    "thinking": "",
                    "tools": [],
                    "role": "user",
                }
                transcript.append(user_entry)
                # User messages go only to the moderator — the moderator
                # decides how to act (route to others, answer directly, etc.)
                pending.setdefault(moderator, []).append(user_entry)

            # Background-work nudges from the bg monitors (system_queue).
            _drain_system_notes(pump, transcript, pending, moderator)

        # Meeting concluded
        await event_queue.put(CommonEvent(type=SYSTEM, data={
            "subtype": wire.SUBTYPE_MEETING_CONCLUDED,
            "meeting_id": meeting_id,
            "total_turns": total_turns,
            "cost_usd": meeting_total_cost,
        }))

    except asyncio.CancelledError:
        logger.info(f"Meeting {meeting_id} cancelled")
        await event_queue.put(CommonEvent(type=SYSTEM, data={
            "subtype": wire.SUBTYPE_MEETING_CONCLUDED,
            "meeting_id": meeting_id,
            "total_turns": total_turns,
            "cost_usd": meeting_total_cost,
            "cancelled": True,
        }))
    except Exception as e:
        logger.error(f"Meeting {meeting_id} producer error: {e}", exc_info=True)
        await event_queue.put(CommonEvent(type=SYSTEM, data={
            "subtype": wire.SUBTYPE_MEETING_CONCLUDED,
            "meeting_id": meeting_id,
            "total_turns": total_turns,
            "error": str(e)[:200],
        }))
    finally:
        await event_queue.put(CommonEvent(type=PRODUCER_DONE, data={}))


# ---------------------------------------------------------------------------
# Pre-pump failure surfacing
# ---------------------------------------------------------------------------

# How long the failure pump lingers before PRODUCER_DONE: the dashboard WS
# polls an idle chat every 3 s (_task_pump_poll) and, on finding an active
# pump, re-sends chat history — which carries the persisted meeting_failed
# banner and the cleared restore.meeting (status is already failed).
_FAIL_PUMP_LINGER_S = 10.0


async def fail_meeting(meeting_id: str, reason: str) -> None:
    """A meeting that will not run (its convener lost the agent): marked
    failed with the reason and surfaced into the requesting chat."""
    await _notify_meeting_failed(meeting_id, reason)


async def _notify_meeting_failed(meeting_id: str, reason: str) -> None:
    """Surface a pre-pump meeting failure into the requesting chat.

    Every early-exit path in start_meeting() (usage pre-check, config build,
    admission denial, participant spawn) used to fail the meeting with only a
    proxy log line — the moderator's ack turn had already told the user the
    meeting was set up, and the chat kept a live-looking meeting pill with no
    error. Persist a ``meeting_failed`` system event through a minimal
    meeting pump so an open chat attaches and shows the reason, and mark the
    row failed (summary = reason, shown on the Meetings tabs). Best-effort:
    surfacing must never mask the original failure path.
    """
    try:
        await run_db(task_store.update_meeting, meeting_id, status=meeting_status.FAILED,
                     summary=(reason or "")[:5000])
    except Exception:
        logger.exception(f"Meeting {meeting_id}: failed-status update failed")
    try:
        meeting = await run_db(task_store.get_meeting, meeting_id)
        if not meeting:
            return
        parent_chat_id = meeting["parent_chat_id"]
        # The usage pre-check fires while the moderator's ack turn may still
        # be streaming — same bounded wait as the normal path before taking
        # the chat's pump registration.
        existing_pump = _active_pumps.get(parent_chat_id)
        if existing_pump and not existing_pump.is_done:
            # Keep TimeoutError + Exception (a doomed prior pump must not block
            # the failure path) but let CancelledError propagate — swallowing it
            # would keep a superseded/aborted meeting running.
            with contextlib.suppress(asyncio.TimeoutError, Exception):
                await asyncio.wait_for(existing_pump._task, timeout=120.0)
            await asyncio.sleep(0.5)

        pump_session_id = "meeting-" + meeting_id
        event_queue: asyncio.Queue = asyncio.Queue()

        async def _produce_failure() -> None:
            await event_queue.put(CommonEvent(type=SYSTEM, data={
                "subtype": wire.SUBTYPE_MEETING_FAILED,
                "meeting_id": meeting_id,
                "message": reason,
            }))
            await asyncio.sleep(_FAIL_PUMP_LINGER_S)
            await event_queue.put(CommonEvent(type=PRODUCER_DONE, data={}))

        pump = ChatStreamPump(
            chat_id=parent_chat_id,
            session_id=pump_session_id,
            producer=None,
            event_queue=event_queue,
            perm_queue=get_permission_queue(pump_session_id),
            scope=meeting.get("scope", "user"),
            source_type=session_kind.MEETING.source_type,
        )
        pump.producer = asyncio.create_task(_produce_failure())
        _active_pumps[parent_chat_id] = pump
        try:
            pump.start()
            await pump._task
        finally:
            cleanup_session_permission_state(pump_session_id)
    except Exception:
        logger.exception(f"Meeting {meeting_id}: failure surfacing failed")


# ---------------------------------------------------------------------------
# Orchestrator entry point
# ---------------------------------------------------------------------------

async def start_meeting(meeting_id: str) -> None:
    """Start the meeting orchestrator."""
    meeting = await run_db(task_store.get_meeting, meeting_id)
    if not meeting:
        logger.error(f"Meeting {meeting_id} not found")
        return
    if meeting["status"] != meeting_status.PENDING:
        logger.error(f"Meeting {meeting_id} status is {meeting['status']}, expected pending")
        return

    # ── Usage limit pre-check (mirrors the scheduler + chat gates) ──
    # Agent-scope meetings spend the platform pool under the host (parent-chat)
    # agent's budget; user-scope meetings draw on the creator's platform-auth
    # budget. Block before spawning any session if already over. Best-effort —
    # the reason surfaces on the meeting (summary + failed status).
    try:
        from services.billing import pool_caps, usage_service
        from services.engines import subscription_pool
        m_scope = meeting.get("scope", "user")
        blocked_reason = ""
        host_chat = await run_db(task_store.get_chat, meeting["parent_chat_id"]) or {}
        host_agent = host_chat.get("agent") or meeting.get("moderator", "")
        if m_scope == "agent":
            if host_agent:
                ls = await asyncio.to_thread(usage_service.check_agent_limit, host_agent)
                if not ls["allowed"]:
                    blocked_reason = f"Agent '{host_agent}' usage limit exceeded"
        elif meeting.get("created_by"):
            creator = await run_db(task_store.get_user, meeting["created_by"])
            creator_role = (creator or {}).get("role", "member")
            ls = await asyncio.to_thread(
                usage_service.check_user_limit, meeting["created_by"], creator_role
            )
            if not ls["allowed"]:
                blocked_reason = "User usage limit exceeded"
        # The subscription pool cap, on the host agent's engine (a participant
        # on another engine is refused by the pool at its own spawn).
        if not blocked_reason and host_agent:
            host_layer = ((agent_store.get_agent(host_agent) or {}).get(
                "execution_path") or DEFAULT_EXECUTION_PATH)
            if m_scope == "agent":
                cap = await asyncio.to_thread(pool_caps.evaluate, "platform", "", host_layer)
                cap_sub = None
            else:
                cap_sub = meeting.get("created_by") or ""
                cap = await asyncio.to_thread(pool_caps.evaluate, "user", cap_sub, host_layer)
            if not cap.allowed and (
                cap.on_reached != "continue"
                or not await asyncio.to_thread(
                    subscription_pool.cap_continue_available, host_layer, cap_sub)
            ):
                blocked_reason = cap.short_reason()
        if blocked_reason:
            logger.warning(f"Meeting {meeting_id} blocked by usage limit: {blocked_reason}")
            await _notify_meeting_failed(meeting_id, blocked_reason)
            return
    except Exception as e:
        logger.error(f"Meeting {meeting_id} usage limit pre-check failed: {e}")

    # Pending to active in one conditional write: a second start of the same
    # meeting, or a failure written while the checks above awaited (the
    # offboarding's fail_meeting), leaves the row elsewhere and this start
    # runs nothing.
    if not await run_db(task_store.update_meeting_if, meeting_id,
                        (meeting_status.PENDING,), status=meeting_status.ACTIVE):
        logger.info(f"Meeting {meeting_id}: no longer pending, not started")
        return
    parent_chat_id = meeting["parent_chat_id"]
    participants = json.loads(meeting["participants"])

    # Resolve parent session ID — used for permission mode inheritance.
    # Meeting agents' hooks check get_session_mode(parent_session_id) to
    # inherit the chat's permission mode (default/acceptEdits/dontAsk).
    parent_session_id = meeting.get("parent_session_id") or ""

    # Wait for any existing pump on this chat to finish (e.g., the agent that
    # called start_meeting is still generating its response). We must not
    # register the meeting pump until the old pump is gone.
    existing_pump = _active_pumps.get(parent_chat_id)
    if not parent_session_id and existing_pump:
        parent_session_id = existing_pump.session_id or ""
    if existing_pump and not existing_pump.is_done:
        logger.info(f"Meeting {meeting_id}: waiting for existing pump on {parent_chat_id[:8]} to finish")
        # Keep TimeoutError + Exception (a doomed prior pump must not block
        # meeting setup) but let CancelledError propagate — swallowing it would
        # keep a superseded/aborted meeting running.
        with contextlib.suppress(asyncio.TimeoutError, Exception):
            await asyncio.wait_for(existing_pump._task, timeout=120.0)
        # Brief pause for WS to process pump_ended
        await asyncio.sleep(0.5)

    # Create sessions for each participant (parallel for speed)
    agent_sessions: dict[str, str] = {}

    # Pre-allocate session IDs.
    from core.concurrency import acquire_meeting_slots, release_meeting_slots
    session_id_map = {slug: str(uuid.uuid4()) for slug in participants}
    all_sids = list(session_id_map.values())

    # Pass 1: build every participant's config — which RESOLVES each execution
    # target — BEFORE acquiring slots, so we reserve only the LOCAL participants
    # against the local ceiling G (a participant routed to a satellite is bounded
    # by THAT satellite's budget, not the proxy's). Building a config spawns
    # nothing and takes no slot, so it is safe before the atomic acquire — but
    # it DOES take a pool seat per participant (the subscription acquire lives
    # in the builder), which only start_session's bind lets a close release:
    # every exit below that abandons a built config returns its seat
    # (config_builder.release_config_seat).
    from core.config.config_builder import release_config_seat
    _built = await asyncio.gather(
        *[build_meeting_agent_config(slug, meeting, session_id_map[slug])
          for slug in participants],
        return_exceptions=True,
    )
    _build_err = next((r for r in _built if isinstance(r, BaseException)), None)
    if _build_err is not None:
        from core.sandbox.session_config_dir import AgentStateRefused
        from services.engines.subscription_pool import NoSubscriptionError
        refused = isinstance(_build_err, AgentStateRefused)
        if refused:
            logger.info(f"Meeting {meeting_id}: a participant is below the editor tier: {_build_err}")
        else:
            logger.error(f"Meeting {meeting_id}: failed to build participant configs: {_build_err}",
                         exc_info=_build_err)
        for slug, r in zip(participants, _built):
            if not isinstance(r, BaseException):
                release_config_seat(session_id_map[slug], r)
        if refused:
            # A participant would run as the agent below the editor tier: the
            # refusal is the reason, named with its participant.
            who = next(slug for slug, r in zip(participants, _built) if r is _build_err)
            await _notify_meeting_failed(
                meeting_id, f"The meeting could not start: {who}: {_build_err}")
        elif isinstance(_build_err, NoSubscriptionError):
            # The pool refused a participant (its subscription cap, or a
            # user-scope block): the reason is the message, not a crash.
            await _notify_meeting_failed(
                meeting_id, f"The meeting could not start: {_build_err}")
        else:
            await _notify_meeting_failed(
                meeting_id,
                f"The meeting could not start: failed to prepare participant sessions ({str(_build_err)[:200]}).",
            )
        return
    agent_cfgs = dict(zip(participants, _built))
    sid_targets = {session_id_map[slug]: (cfg.execution_target or placement.LOCAL)
                   for slug, cfg in agent_cfgs.items()}
    sid_paths = {session_id_map[slug]: (cfg.execution_path or "")
                 for slug, cfg in agent_cfgs.items()}
    # A Direct-LLM participant reserves for its stdio MCPs.
    sid_mcps = {session_id_map[slug]: (cfg.mcp_config_path or "")
                for slug, cfg in agent_cfgs.items()}

    # Atomic-N reserve of the LOCAL participants only (remote ones are enforced
    # by their satellites). All-or-nothing for the local subset.
    adm = await acquire_meeting_slots(all_sids, targets=sid_targets, exec_paths=sid_paths,
                                      mcp_paths=sid_mcps)
    if not adm:
        logger.error(f"Meeting {meeting_id}: denied — {adm.user_message}")
        for slug, cfg in agent_cfgs.items():
            release_config_seat(session_id_map[slug], cfg)
        await _notify_meeting_failed(
            meeting_id,
            adm.user_message or "The platform cannot start the meeting right now.",
        )
        return

    async def _create_session(agent_slug: str, sid: str) -> tuple[str, str]:
        agent_cfg = agent_cfgs[agent_slug]  # built in pass 1 (target already resolved)
        # Thread the meeting creator's user_sub + role into the layer resolver,
        # exactly like the chat (ws/dashboard.py) and task (scheduler.py) paths.
        # build_meeting_agent_config already resolved execution_target WITH the
        # creator's sub (user-paired override), but get_execution_layer re-runs
        # the per-user satellite isolation guard against that target. Without
        # user_sub the guard treats this as an agent-scope session and refuses
        # to run on a user-paired machine — so a user-scoped meeting whose
        # participant is routed to the creator's OWN user-paired machine fails
        # ("Agent-scope sessions cannot run on user-paired remote machines")
        # before any meeting pump exists, leaving the dashboard with nothing to
        # attach to. Agent-scope meetings keep an empty user_sub (→ None), so
        # the guard still correctly refuses them on user-paired machines.
        layer = get_execution_layer(
            agent_slug,
            execution_path=agent_cfg.execution_path,
            user_sub=agent_cfg.user_sub or None,
            role=getattr(agent_cfg.security_context, "role", roles.MANAGER) or roles.MANAGER,
            execution_target=agent_cfg.execution_target,
        )
        _meeting_session_layers[sid] = layer
        await layer.start_session(sid, agent_cfg)
        return agent_slug, sid

    try:
        # return_exceptions=True is load-bearing twice over: a plain gather
        # (a) raises on the first failure while sibling spawns KEEP RUNNING —
        # they would start_session() after the rollback below has already
        # released their reservations — and (b) leaves no record of which
        # participants DID spawn. Waiting for every outcome makes the rollback
        # complete and race-free.
        results = await asyncio.gather(
            *[_create_session(slug, session_id_map[slug]) for slug in participants],
            return_exceptions=True,
        )
        first_err = next((r for r in results if isinstance(r, BaseException)), None)
        if first_err is not None:
            raise first_err
        pump_session_id = "meeting-" + meeting_id
        for agent_slug, sid in results:
            agent_sessions[agent_slug] = sid
            _sessions.setdefault(sid, {"created": True, "message_count": 0})
            _sessions[sid].update({
                "is_task": True,
                "is_meeting": True,
                "client_type": session_kind.MEETING.name,
                "agent": agent_slug,
                "last_active": now_iso(),
                "meeting_id": meeting_id,
            })
            # Register for the hook route resolver: permission-mode inheritance
            # plus out-of-band event rebinding to the pump queue + parent chat
            # (is_moderator: only the moderator's end_meeting ends a turn).
            set_meeting_session_info(sid, parent_session_id, pump_session_id,
                                     agent_slug, parent_chat_id,
                                     is_moderator=(agent_slug == meeting["moderator"]))
            # Bind background-work tracking to the parent chat from the start
            # (re-stamped after every turn — the CLI layer resets per turn).
            get_subagent_registry(sid).chat_id = parent_chat_id
            get_bg_command_registry(sid).chat_id = parent_chat_id
        _save_sessions()

    except Exception as e:
        logger.error(f"Meeting {meeting_id}: failed to create sessions: {e}", exc_info=True)
        # Roll back from the per-spawn layer map — NOT agent_sessions, which is
        # populated only after a fully-successful gather and is always empty
        # here (the pre-fix rollback closed nothing, orphaning every sibling
        # that spawned before the failing participant).
        from services.engines import subscription_pool as _pool
        for slug in participants:
            sid = session_id_map[slug]
            layer = _meeting_session_layers.pop(sid, None)
            # A participant that spawned and bound owns its seat: closing it
            # releases it. One that never bound (never reached start_session,
            # or whose start_session raised) still holds the build's seat.
            was_bound = _pool.session_bound(sid)
            if layer is not None:
                with contextlib.suppress(Exception):
                    await layer.close_session(sid)
            if not was_bound:
                release_config_seat(sid, agent_cfgs[slug])
        release_meeting_slots(all_sids)
        await _notify_meeting_failed(
            meeting_id,
            f"The meeting could not start: participant sessions failed to start ({str(e)[:200]}).",
        )
        return

    # meeting_started event is emitted through the pump (first event in producer)
    # so the WS receives it live when it attaches.

    # Build participant data for the meeting_started event
    _meeting_participants = [
        {
            "slug": slug,
            "display_name": (agent_store.get_agent(slug) or {}).get("display_name", slug),
            "color": (agent_store.get_agent(slug) or {}).get("color", ""),
        }
        for slug in participants
    ]

    # Create pump and producer
    event_queue: asyncio.Queue = asyncio.Queue()

    pump = ChatStreamPump(
        chat_id=parent_chat_id,
        session_id=pump_session_id,
        producer=None,
        event_queue=event_queue,
        perm_queue=get_permission_queue(pump_session_id),
        scope=meeting["scope"],
        source_type=session_kind.MEETING.source_type,
    )

    producer_task = asyncio.create_task(
        meeting_produce(meeting_id, agent_sessions, event_queue, pump,
                        meeting_participants=_meeting_participants)
    )
    pump.producer = producer_task
    pump.system_queue_consumer = True  # _drain_system_notes reads it each round
    pump.queue_closed = False  # meeting_produce reads the user messages each round

    _active_pumps[parent_chat_id] = pump
    _active_meetings[meeting_id] = asyncio.current_task()

    try:
        pump.start()
        await pump._task
    finally:
        _active_meetings.pop(meeting_id, None)

        turns = await run_db(task_store.get_meeting_turns, meeting_id)
        total_cost = sum(t.get("cost_usd", 0) for t in turns)

        for slug, sid in agent_sessions.items():
            cleanup_meeting_session_info(sid)
            try:
                layer = _meeting_session_layers.get(sid) or get_execution_layer(slug)
                await layer.close_session(sid)
            except Exception:
                pass
            _meeting_session_layers.pop(sid, None)
        release_meeting_slots(list(agent_sessions.values()))
        # Clean up the meeting pump's permission queue
        cleanup_session_permission_state(pump_session_id)

        # Extract moderator's final summary from the last moderator turn
        meeting = await run_db(task_store.get_meeting, meeting_id)
        moderator = (meeting or {}).get("moderator", "")
        mod_turns = [t for t in turns if t.get("agent") == moderator and t.get("content")]
        meeting_summary = mod_turns[-1]["content"] if mod_turns else ""

        # One conditional write: a row already failed or concluded keeps it.
        if meeting:
            await run_db(
                task_store.update_meeting_if, meeting_id,
                meeting_status.STATUSES - meeting_status.TERMINAL,
                status=meeting_status.CONCLUDED,
                concluded_at=now_iso(),
                cost_usd=total_cost,
                summary=meeting_summary[:5000],
            )

        # Usage recording: handled by the pump at PRODUCER_DONE (source_type="meeting").
        # The pump's _total_cost_delta includes meeting per-turn deltas, and it records
        # one usage_records entry. No separate recording here to avoid double-counting.

        logger.info(f"Meeting {meeting_id} concluded, cost=${total_cost:.4f}")


# ---------------------------------------------------------------------------
# Meeting control functions
# ---------------------------------------------------------------------------

async def end_meeting(meeting_id: str, agent_slug: str | None = None) -> dict:
    meeting = await run_db(task_store.get_meeting, meeting_id)
    if not meeting:
        return {"error": "Meeting not found"}
    if meeting["status"] not in meeting_status.ENDABLE:
        return {"error": "Meeting not active"}
    if agent_slug and agent_slug != meeting["moderator"]:
        return {"error": "Only the moderator can end the meeting"}
    # The status may have moved since the read: the write is conditional.
    if not await run_db(task_store.update_meeting_if, meeting_id, meeting_status.ENDABLE,
                        status=meeting_status.CONCLUDING):
        return {"error": "Meeting not active"}
    return {"status": meeting_status.CONCLUDING, "meeting_id": meeting_id}


async def leave_meeting(meeting_id: str, agent_slug: str, reason: str = "") -> dict:
    meeting = await run_db(task_store.get_meeting, meeting_id)
    if not meeting:
        return {"error": "Meeting not found"}
    if meeting["status"] not in meeting_status.LEAVABLE:
        return {"error": "Meeting not active"}
    if agent_slug not in json.loads(meeting["active_participants"]):
        return {"error": "Agent not in meeting"}
    # Under the row's lock: two agents leaving at once both leave.
    active = await run_db(task_store.remove_active_participant, meeting_id, agent_slug,
                          meeting_status.LEAVABLE)
    if active is None:
        return {"error": "Meeting not active"}
    return {"status": "left", "meeting_id": meeting_id, "agent": agent_slug, "remaining": len(active)}
