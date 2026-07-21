"""Task producer — feeds CommonEvent objects to a ChatStreamPump for task execution.

Handles the settle + background agent nudge flow within the pump's event stream.
All events go through the pump → saved as chat_messages → rich structured output.

Also broadcasts a simplified per-event SSE stream consumed by the schedules-mcp's
``run_task(wait=true)`` (``GET /v1/tasks/runs/{run_id}/stream``), so MCPs that
spawn tasks can stream live output back to the calling agent without
re-implementing chat-message decoding.
"""

import asyncio
import contextlib
import logging
import time

from core.events.common_events import (
    CommonEvent, SUBAGENT_START, BG_COMMAND_START, TEXT, TOOL_USE, TOOL_RESULT,
    DONE, ERROR, QUEUE_TURN, PRODUCER_DONE,
)
from core.execution_layer import ExecutionLayer
from core.session.session_state import get_subagent_registry
from core.events.bg_command_state import get_bg_command_registry
from core.events.pump_bg_monitors import hold_bg_monitors

logger = logging.getLogger("claude-proxy")


# ---------------------------------------------------------------------------
# SSE broadcast bridge for the schedules-mcp ``/v1/tasks/runs/{run_id}/stream``
# endpoint. Producers populate ``_run_subscribers`` queues so each event is
# fanned out to the MCP-side stream consumer in addition to the chat pump.
# ---------------------------------------------------------------------------

def _event_to_sse(event: CommonEvent) -> dict | None:
    """Convert a CommonEvent to SSE broadcast format for _run_subscribers."""
    if event.type == TEXT:
        content = event.data.get("content", "")
        if content:
            return {"type": "text", "text": content}

    elif event.type == TOOL_USE:
        return {
            "type": "tool_start",
            "name": event.data.get("name", ""),
            "summary": "",
        }

    elif event.type == TOOL_RESULT:
        return {
            "type": "tool_end",
            "name": event.data.get("name", ""),
        }

    elif event.type == SUBAGENT_START:
        return {
            "type": "task_spawn",
            "subagent_type": event.data.get("subagent_type", ""),
        }

    elif event.type == DONE:
        # Don't emit SSE done here — the producer sends it explicitly at the end
        return None

    return None


# ---------------------------------------------------------------------------
# Task producer coroutine
# ---------------------------------------------------------------------------

async def task_produce(
    layer: ExecutionLayer,
    session_id: str,
    prompt: str,
    event_queue: asyncio.Queue,
    run_id: str,
    broadcast_fn=None,
    settle_timeout: float = 30.0,
    bg_poll: float = 2.0,
    bg_cmd_ceiling: float = 600.0,
    bg_sub_ceiling: float = 1800.0,
    bg_stall_nudge: float = 120.0,
    bg_max_rounds: int = 10,
) -> None:
    """Producer for task execution — sends prompt through ExecutionLayer, routes to pump.

    After the main turn, waits (bounded) for background work and nudges a
    review turn for what actually COMPLETED — never for still-running work.
    Broadcasts events to SSE subscribers for the schedules-mcp stream consumer.

    Args:
        layer: ExecutionLayer for the agent.
        session_id: Session ID.
        prompt: Task prompt text.
        event_queue: Queue for CommonEvent objects → ChatStreamPump reads these.
        run_id: Task run ID (for SSE broadcast).
        broadcast_fn: Optional async fn(run_id, event_dict) for SSE broadcast.
        settle_timeout: Seconds to wait for background agents in settle mode.
        bg_poll: Wait/drain slice for the post-turn bg-review phase.
        bg_cmd_ceiling: Give-up horizon for still-running bash commands (their
            only completion signal is a stdout frame — chat-monitor parity).
        bg_sub_ceiling: Give-up horizon for still-running bg subagents (their
            SubagentStop hook is deterministic, so wait generously — a
            legitimately long delegated bg agent must not be cut short; the
            ceiling only bounds a genuinely lost hook).
        bg_stall_nudge: With completed work in hand and ZERO progress for this
            long, nudge what completed instead of waiting out a stuck sibling.
        bg_max_rounds: Runaway belt on review rounds (each round requires new
            completions or new spawns; real workflows use 1-3).
    """
    bg_count = 0
    bgcmd_count = 0

    async def _broadcast(event: CommonEvent):
        """Forward event to SSE subscribers if broadcast_fn provided."""
        if broadcast_fn is None:
            return
        sse_evt = _event_to_sse(event)
        if sse_evt:
            with contextlib.suppress(Exception):
                await broadcast_fn(run_id, sse_evt)

    try:
        # Suppress the chat-path bg monitors for the whole run: this producer
        # owns bg-completion handling, and a dashboard viewer detaching mid-run
        # would otherwise arm a monitor on the task session that races the
        # drains below and double-nudges.
        with hold_bg_monitors(session_id):
            # Main turn with settle (waits for background agents at CLI level).
            # The lock is taken PER send — never across the whole flow: the
            # post-turn drains below timeout-acquire this same lock inside the
            # layer (drain_bg_commands), so holding it across the phase would
            # no-op them forever. A user message into the task chat may
            # therefore interleave between producer turns — its turn surfaces
            # any resolved bg completions itself (the CLI injects the
            # task-notification into the live turn), which is why the
            # registry's per-turn reset clearing `unsurfaced` is correct here.
            async with layer.session_lock(session_id):
                async for event in layer.send_message(
                    session_id, prompt, settle_after_result=settle_timeout,
                ):
                    await event_queue.put(event)
                    await _broadcast(event)

                    # Count background work (for the nudge wording + cohort).
                    # Their completion is tracked deterministically in the
                    # registries (subagents via SubagentStop hooks; bash
                    # commands via the task_updated frame the settle loop
                    # drains) — not by counting notifications here.
                    if (event.type == SUBAGENT_START
                            and event.data.get("run_in_background")):
                        bg_count += 1
                    elif event.type == BG_COMMAND_START:
                        bgcmd_count += 1

            # Background work — the delegation contract REQUIRES a delegated
            # agent's result return only after its bg sub-agents AND bg shell
            # commands finished AND it synthesized. Layer timings, one uniform
            # handling:
            #   CLI   — settle already drained both before send_message returned
            #           (subagents via SubagentStop; commands via task_updated on
            #           stdout), so they show up only as the *_count tallies and
            #           both registries read 0 here.
            #   Codex — the main turn ends while bg subs keep running on their own
            #           threads, so they're still pending in the subagent registry
            #           here (Codex has no background bash).
            # Wait for whichever cohort is outstanding, then nudge a review of
            # what COMPLETED. Loop so a synthesis turn that itself spawns MORE
            # bg work is also awaited. Every wait is BOUNDED and the nudge
            # never claims still-running work "completed": a never-exiting
            # shell once held a run in a 2-minute false-nudge loop for hours
            # (144 turns) until a manual abort.
            reg = get_subagent_registry(session_id)
            bgreg = get_bg_command_registry(session_id)
            # Subagents keep the spawn-tally contract (any bg spawn forces a
            # review turn — Codex subs are genuinely pending here, and a CLI
            # sub's report may postdate the model's final text). Bash commands
            # are exempt when every completion was already surfaced to the
            # model DURING generation (the CLI injects the task-notification
            # into the live turn): only pending or UNSURFACED completions
            # (settle-phase / post-turn resolves) force the review turn.
            cohort = (
                (bg_count or reg.pending_count)
                + bgreg.pending_count + bgreg.unsurfaced_count
            )
            # Per-type give-up horizons, re-armed on ANY observed completion
            # and after each nudge turn. Once a type exceeds its ceiling it is
            # abandoned: dropped from the cohort so the loop can't re-wait it.
            now = time.monotonic()
            sub_deadline = now + bg_sub_ceiling
            cmd_deadline = now + bg_cmd_ceiling
            abandoned_subs = False
            abandoned_cmds = False
            session_gone = False
            rounds = 0
            while cohort > 0:
                rounds += 1
                if rounds > bg_max_rounds:
                    logger.warning(
                        f"Task {run_id[:8]}: bg review exceeded "
                        f"{bg_max_rounds} rounds — stopping "
                        f"(agents pending={reg.pending_count}, "
                        f"commands pending={bgreg.pending_count})"
                    )
                    break
                if reg.pending_count or bgreg.pending_count:
                    logger.info(
                        f"Task {run_id[:8]}: waiting on bg work "
                        f"(agents={reg.pending_count}, "
                        f"commands={bgreg.pending_count})"
                    )
                last_progress = time.monotonic()
                snapshot = (reg.pending_count, bgreg.pending_count,
                            bgreg.unsurfaced_count)
                while True:
                    subs_pending = 0 if abandoned_subs else reg.pending_count
                    cmds_pending = 0 if abandoned_cmds else bgreg.pending_count
                    if not subs_pending and not cmds_pending:
                        break
                    now = time.monotonic()
                    if subs_pending and now >= sub_deadline:
                        abandoned_subs = True
                        logger.warning(
                            f"Task {run_id[:8]}: {subs_pending} bg agent(s) "
                            f"still pending at the {bg_sub_ceiling:.0f}s "
                            f"ceiling (lost SubagentStop?) — giving up on them"
                        )
                        continue
                    if cmds_pending and now >= cmd_deadline:
                        abandoned_cmds = True
                        logger.warning(
                            f"Task {run_id[:8]}: {cmds_pending} bg command(s) "
                            f"still running at the {bg_cmd_ceiling:.0f}s "
                            f"ceiling — giving up on them (no nudge for "
                            f"still-running work)"
                        )
                        continue
                    if not await layer.is_session_alive(session_id):
                        logger.info(
                            f"Task {run_id[:8]}: session gone during bg wait"
                        )
                        session_gone = True
                        break
                    if subs_pending:
                        # Hook-driven — observable while the CLI is idle.
                        await layer.wait_for_bg_subagents(
                            session_id, timeout=bg_poll,
                        )
                    if cmds_pending:
                        # A command's ONLY completion signal is a stdout frame
                        # nobody reads between turns — actively drain (the
                        # layer timeout-acquires the session lock; remote
                        # pulls satellite-pushed frames from its event queue).
                        progressed = await layer.drain_bg_commands(
                            session_id, budget=bg_poll,
                        )
                        if bgreg.pending_count and not progressed:
                            await asyncio.sleep(min(1.0, bg_poll / 2))
                    current = (reg.pending_count, bgreg.pending_count,
                               bgreg.unsurfaced_count)
                    if current != snapshot:
                        snapshot = current
                        last_progress = time.monotonic()
                        sub_deadline = last_progress + bg_sub_ceiling
                        cmd_deadline = last_progress + bg_cmd_ceiling
                    # Completed work in hand + a stalled sibling: review what
                    # finished instead of holding its results hostage.
                    nudgeable = (
                        (bg_count and reg.pending_count == 0)
                        or bgreg.unsurfaced_count
                    )
                    if (nudgeable
                            and time.monotonic() - last_progress
                            >= bg_stall_nudge):
                        break
                if session_gone:
                    break

                bits = []
                # Agents join the nudge only once EVERY one of them finished
                # (their reports are per-cohort); commands join per completion.
                if bg_count and reg.pending_count == 0:
                    bits.append(f"{bg_count} background agent(s)")
                bash_unseen = bgreg.unsurfaced_count
                if bash_unseen:
                    bits.append(f"{bash_unseen} background command(s)")
                if not bits:
                    # Nothing completed-but-unseen — only abandoned/pending
                    # work remains. Never nudge for that: the message would
                    # be false and the loop unbounded (the 144-nudge bug).
                    if reg.pending_count or bgreg.pending_count:
                        logger.warning(
                            f"Task {run_id[:8]}: finishing with bg work "
                            f"still running (agents={reg.pending_count}, "
                            f"commands={bgreg.pending_count}) — no nudge"
                        )
                    break
                what = " and ".join(bits)
                nudge = (
                    f"Your {what} have completed their work. "
                    f"Please review the output and continue with the task."
                )
                logger.info(f"Task {run_id[:8]}: sending bg completion nudge ({what})")
                # The review turn we are about to send surfaces them.
                bgreg.clear_unsurfaced()
                bg_count = 0  # reviewed — later rounds re-tally new spawns

                # Emit as queued user message so pump shows it as a turn boundary
                await event_queue.put(
                    CommonEvent(type=QUEUE_TURN, data={"text": nudge})
                )

                async with layer.session_lock(session_id):
                    async for event in layer.send_message(session_id, nudge):
                        await event_queue.put(event)
                        await _broadcast(event)
                        # A synthesis turn can spawn MORE bg work — keep
                        # tallies fresh so the loop waits for it too.
                        if (event.type == SUBAGENT_START
                                and event.data.get("run_in_background")):
                            bg_count += 1
                        elif event.type == BG_COMMAND_START:
                            bgcmd_count += 1

                # A nudge is progress — re-arm the horizons for work the
                # review turn itself spawned.
                now = time.monotonic()
                sub_deadline = now + bg_sub_ceiling
                cmd_deadline = now + bg_cmd_ceiling
                # Re-check both registries; loop until a turn leaves none
                # pending (abandoned work excluded) and no completion landed
                # unseen after its final text.
                cohort = (
                    (bg_count or (0 if abandoned_subs else reg.pending_count))
                    + (0 if abandoned_cmds else bgreg.pending_count)
                    + bgreg.unsurfaced_count
                )

        # Broadcast SSE done
        if broadcast_fn:
            with contextlib.suppress(Exception):
                await broadcast_fn(run_id, {"type": "done", "status": "completed"})

    except asyncio.CancelledError:
        # Task was cancelled
        if broadcast_fn:
            with contextlib.suppress(Exception):
                await broadcast_fn(run_id, {"type": "done", "status": "cancelled"})
        raise

    except Exception as e:
        logger.error(f"Task producer error: {e}", exc_info=True)
        await event_queue.put(CommonEvent(type=ERROR, data={"message": str(e)}))
        if broadcast_fn:
            with contextlib.suppress(Exception):
                await broadcast_fn(run_id, {"type": "done", "status": "failed"})

    finally:
        await event_queue.put(CommonEvent(type=PRODUCER_DONE, data={}))
