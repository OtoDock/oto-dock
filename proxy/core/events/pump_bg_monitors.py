"""Background-work monitors for dashboard chat turns.

After a turn leaves background subagents or bash commands running, these
monitors wait for completion (deterministic SubagentRegistry signal for
subagents; active stdout drain for bash commands) and nudge the LLM to review
the results. Extracted from stream_pump.py; stream_pump re-exports the public
entry points so existing imports keep working.
"""

import asyncio
import json
import logging
import time
from contextlib import contextmanager

from storage import database as task_store
from core.session.session_state import (
    _dashboard_notify_queues,
    push_pump_event,
    queue_pump_prompt,
    mark_bg_agents_completed,
    get_subagent_registry,
)
from core.events.bg_command_state import get_bg_command_registry

logger = logging.getLogger("claude-proxy")


def format_job_list(labels: list[str], limit: int = 3) -> str:
    """Render finished-job labels for a nudge: `a; b (+2 more)`.

    Labels come from the registries (composed at registration by the layer
    that knows what the model can see — see BackgroundCommandRegistry.labels).
    Empty/missing labels are dropped; an all-empty list renders "" and the
    nudge degrades to its count-only form."""
    named = [l for l in labels if l]
    shown = named[:limit]
    out = "; ".join(shown)
    if len(named) > len(shown):
        out += f" (+{len(named) - len(shown)} more)"
    return out


def compose_agent_nudge(count: int, labels: list[str] | None = None) -> str:
    """The bg-subagent completion nudge — THE single composition point
    (the WS notify path in dashboard_server_events re-composes with this)."""
    listed = format_job_list(labels or [])
    if listed:
        return (f"Your {count} background agent(s) have completed: {listed}. "
                f"Please review the results and continue.")
    return (f"Your {count} background agent(s) have completed. "
            f"Please review the results and continue.")


def compose_command_nudge(count: int, labels: list[str] | None = None) -> str:
    """The bg-command completion nudge — single composition point (see
    compose_agent_nudge)."""
    listed = format_job_list(labels or [])
    if listed:
        return (f"Your {count} background command(s) have finished: {listed}. "
                f"Review their output and continue with the task.")
    return (f"Your {count} background command(s) have finished. "
            f"Review their output and continue with the task.")


_bg_monitors_running: set[str] = set()  # session_ids with an active bg-agent monitor

# Grace window for the CLI's OWN self-wake review turn (Claude ≥2.1.243):
# after a background cohort resolves, the CLI wakes itself and reviews the
# completion — our nudge is the FALLBACK now, fired only when no wake turn
# was captured within this window. Event-driven (no CLI-version gate): older
# engines never wake, the grace expires, the nudge fires as before.
WAKE_GRACE_S = 15.0


async def _wake_grace_covers(layer, session_id: str) -> bool:
    """True when a captured self-wake turn covers the just-resolved cohort —
    the nudge stands down. Actively drains during the grace (the drain
    records wake brackets — core/events/wake_capture.py): the subagent path
    has no other between-turns stdout reader, so a passive wait could never
    observe the wake. Claude sessions only (``layer.session_self_wakes``);
    codex has no self-wake and skips the grace entirely."""
    try:
        wakes = getattr(layer, "session_self_wakes", None)
        if wakes is None or not await wakes(session_id):
            return False
    except Exception:
        return False
    from core.events import wake_capture
    if wake_capture.recently_captured(session_id, within_s=WAKE_GRACE_S):
        return True
    deadline = time.monotonic() + WAKE_GRACE_S
    while time.monotonic() < deadline:
        try:
            await layer.drain_bg_commands(session_id, budget=1.0)
        except Exception:
            logger.debug("wake grace: drain failed for %s", session_id[:8],
                         exc_info=True)
            return False
        if wake_capture.recently_captured(session_id, within_s=WAKE_GRACE_S):
            return True
        await asyncio.sleep(0.5)
    return False


def bg_monitor_running(session_id: str) -> bool:
    """True if a _bg_agent_monitor is already watching this session's cohort."""
    return session_id in _bg_monitors_running


async def _bg_agent_monitor(
    layer, session_id: str, chat_id: str, count: int,
) -> None:
    """Idempotent per session: a turn end can try to launch this from several
    places (normal end, detach to another tab, reconnect mid-bg) — but only ONE
    monitor per session's cohort may run, else the review nudge fires twice.
    Delegates to _bg_agent_monitor_impl under a running-guard."""
    if session_id in _bg_monitors_running:
        return
    _bg_monitors_running.add(session_id)
    try:
        await _bg_agent_monitor_impl(layer, session_id, chat_id, count)
    finally:
        _bg_monitors_running.discard(session_id)


async def _bg_agent_monitor_impl(
    layer, session_id: str, chat_id: str, count: int,
) -> None:
    """After a turn leaves background subagents running, wait for them to
    finish, then nudge the LLM to review their results.

    Completion is DETERMINISTIC: each agent is marked done in the per-session
    SubagentRegistry — by the SubagentStop hook (CLI) or the per-thread bg
    supervisor (Codex) — and this monitor awaits the registry's all-done event
    (idle-safe — the CLI hook fires over HTTP even while the `-p` process is
    idle, e.g. while a subagent is mid-`sleep`). We do NOT infer completion from
    hook-activity silence: a sleeping/slow subagent produces no hooks and would
    be falsely read as "done", firing a premature nudge. The MAX_WAIT ceiling is
    the only backstop for a genuinely lost completion signal. The monitor does
    NOT bail when the session lock is held by a concurrent user turn (that
    early-exit was removed — it dropped the nudge); it keeps waiting, and the
    nudge is deferred behind the in-flight turn by the natural turn
    serialization. The 3-tier delivery is unchanged.

    Args:
        layer: ExecutionLayer for session operations.
    """
    POLL_INTERVAL = 2.0     # event-wait slice; lock/alive re-checked each slice
    MAX_WAIT = 600.0        # 10 min hard ceiling (lost-SubagentStop backstop)

    reg = get_subagent_registry(session_id)
    reg.chat_id = chat_id
    start = time.monotonic()
    logger.info(f"BG agent monitor started: session={session_id[:8]}, chat={chat_id[:8]}, count={count}")

    settled = False
    while (time.monotonic() - start) < MAX_WAIT:
        # Primary (and only) signal: every spawned subagent has fired its
        # SubagentStop hook → registry all-done event.
        try:
            await asyncio.wait_for(reg.wait_all_done(), timeout=POLL_INTERVAL)
            settled = True
            break
        except asyncio.TimeoutError:
            pass

        if not await layer.is_session_alive(session_id):
            logger.info(f"BG agent monitor: session {session_id[:8]} gone, exiting")
            return
        # Keep waiting — do NOT bail just because the session lock is held (a
        # concurrent user turn): the cohort's completion still needs its nudge,
        # which the turn-start gate defers behind the user's in-flight turn.
        # The bg work is independent of the user's turn. No hook-silence inference.

    if not settled:
        logger.warning(f"BG agent monitor: no SubagentStop all-done within {MAX_WAIT}s (lost hook?), session={session_id[:8]}")
        return

    # Re-check liveness right before delivering. The nudge itself is deferred by
    # the turn-start gate if a user turn is in flight — we never drop it.
    if not await layer.is_session_alive(session_id):
        return

    # The CLI's own self-wake review turn (≥2.1.243) supersedes the nudge —
    # firing ours too would run a second, redundant review at double cost.
    # The cohort's badges still clear (the wake turn was recorded as a real
    # turn; the *_complete cohort frame below keeps reconnect state honest).
    if await _wake_grace_covers(layer, session_id):
        mark_bg_agents_completed(chat_id)
        push_pump_event(chat_id, {"type": "bg_agents_complete", "count": count})
        logger.info(
            f"BG agent monitor: self-wake turn covered the cohort — nudge "
            f"stands down (session={session_id[:8]})"
        )
        return

    # Which agents finished — so the model can tell each completion apart
    # (dogfooding find 2026-08-27: the count-only nudge made an agent read a
    # still-running sibling's output file). Labels were captured at spawn.
    labels = [reg.label_for(t) for t in sorted(reg.completed)]
    nudge = compose_agent_nudge(count, labels)

    # Update live state (for reconnect accuracy)
    mark_bg_agents_completed(chat_id)

    # Path 1: WS connected — notification queue (full handling with UI event + LLM prompt)
    notify_queue = _dashboard_notify_queues.get(session_id)
    if notify_queue:
        push_pump_event(chat_id, {"type": "bg_agents_complete", "count": count})
        await notify_queue.put({
            "type": "bg_nudge",
            "session_id": session_id,
            "chat_id": chat_id,
            "count": count,
            "labels": labels,
        })
        logger.info(f"BG agent monitor: nudge queued for session={session_id[:8]}")
        return

    # Path 2: Pump running (background drain) — queue on pump for in-context delivery
    task_store.add_chat_message(chat_id, "event", "",
        event_type="bg_nudge",
        event_data=json.dumps({"count": count, "labels": labels}))
    if queue_pump_prompt(chat_id, nudge, system=True):
        push_pump_event(chat_id, {"type": "bg_agents_complete", "count": count})
        logger.info(f"BG agent monitor: nudge queued on pump for chat={chat_id[:8]}")
        return

    # Path 3: No pump, no WS — send directly via execution layer
    logger.info(f"BG agent monitor: WS disconnected, delivering directly for session={session_id[:8]}")
    try:
        parts: list[str] = []
        async with layer.session_lock(session_id):
            async for event in layer.send_message(session_id, nudge):
                if event.type == "text":
                    parts.append(event.data.get("content", ""))
        response = "".join(parts)
        if response:
            task_store.add_chat_message(chat_id, "assistant", response)
    except Exception as e:
        logger.error(f"BG agent monitor direct delivery failed: {e}", exc_info=True)


_bg_command_monitors_running: set[str] = set()  # session_ids with an active bg-command monitor


def bg_command_monitor_running(session_id: str) -> bool:
    """True if a _bg_command_monitor is already watching this session's commands."""
    return session_id in _bg_command_monitors_running


@contextmanager
def hold_bg_monitors(session_id: str):
    """Suppress BOTH post-turn monitors for a session while the caller owns
    bg-completion handling itself. The task producer wraps its whole run in
    this: it takes the session lock per send (not across the flow), so a
    dashboard viewer detaching mid-run would otherwise arm a chat monitor on
    the task session, race the producer's own drain, and double-nudge. Adds
    to the monitors' idempotency sets so their guards no-op; releases only
    what it added (a monitor already mid-flight keeps its own guard)."""
    added_agent = session_id not in _bg_monitors_running
    added_cmd = session_id not in _bg_command_monitors_running
    if added_agent:
        _bg_monitors_running.add(session_id)
    if added_cmd:
        _bg_command_monitors_running.add(session_id)
    try:
        yield
    finally:
        if added_agent:
            _bg_monitors_running.discard(session_id)
        if added_cmd:
            _bg_command_monitors_running.discard(session_id)


async def _bg_command_monitor(
    layer, session_id: str, chat_id: str, count: int,
) -> None:
    """Idempotent per session (mirror of _bg_agent_monitor): only ONE bg-command
    monitor per session may run, else the review nudge fires twice."""
    if session_id in _bg_command_monitors_running:
        return
    _bg_command_monitors_running.add(session_id)
    try:
        await _bg_command_monitor_impl(layer, session_id, chat_id, count)
    finally:
        _bg_command_monitors_running.discard(session_id)


async def _bg_command_monitor_impl(
    layer, session_id: str, chat_id: str, count: int,
) -> None:
    """After a turn leaves background bash commands running, detect their
    completion and nudge the LLM to review their output + continue.

    The hard difference from the subagent monitor: a backgrounded bash command
    fires NO completion hook (verified — only PreToolUse/PostToolUse at spawn and
    Stop at turn-end). Its ONLY completion signal is the ``task_updated`` frame on
    stdout. So this monitor ACTIVELY drains the idle session's stdout (under the
    shared session lock, via ``layer.drain_bg_commands``) until every command is
    resolved, then nudges. Each resolved command clears its own badge live
    (``resolve_bg_command`` pushes ``bg_command_done``). The MAX_WAIT ceiling
    backstops a command that genuinely never ends — we do NOT nudge in that case
    (the commands may still be running)."""
    POLL_INTERVAL = 2.0
    MAX_WAIT = 600.0        # 10 min hard ceiling (a never-ending bg command)

    bgreg = get_bg_command_registry(session_id)
    bgreg.chat_id = chat_id
    start = time.monotonic()
    logger.info(
        f"BG command monitor started: session={session_id[:8]}, "
        f"chat={chat_id[:8]}, count={count}"
    )

    while (time.monotonic() - start) < MAX_WAIT:
        if not bgreg.has_pending:
            break
        if not await layer.is_session_alive(session_id):
            logger.info(f"BG command monitor: session {session_id[:8]} gone, exiting")
            return
        # No hook — actively read stdout (briefly, under the session lock) to
        # catch task_updated{completed}. If a user turn holds the lock,
        # drain_bg_commands backs off and returns False (the turn's own
        # translator resolves completions meanwhile); we just retry next poll.
        progressed = await layer.drain_bg_commands(session_id, budget=POLL_INTERVAL)
        if not progressed and bgreg.has_pending:
            await asyncio.sleep(0.3)  # nothing ready — back off before re-draining

    if bgreg.has_pending:
        logger.warning(
            f"BG command monitor: {bgreg.pending_count} command(s) still pending "
            f"after {MAX_WAIT:.0f}s ceiling — giving up (no nudge)"
        )
        return

    if not await layer.is_session_alive(session_id):
        return

    # The user STOPPED this chat's last turn (graceful abort keeps the CLI —
    # and its backgrounded commands — alive, so this monitor now survives an
    # abort): a nudge would auto-run a turn the user just refused. The CLI's
    # own bg tracking hands the results to the model on the next REAL turn.
    if chat_id and (task_store.get_chat(chat_id) or {}).get("last_turn_aborted"):
        logger.info(
            f"BG command monitor: last turn aborted by the user — skipping "
            f"nudge for chat={chat_id[:8]}"
        )
        return

    # Self-wake supersession — mirror of the subagent monitor: the CLI's own
    # review turn (captured by the drain above or during this grace) makes
    # our nudge redundant. Per-command badges are already cleared.
    if await _wake_grace_covers(layer, session_id):
        logger.info(
            f"BG command monitor: self-wake turn covered the cohort — nudge "
            f"stands down (session={session_id[:8]})"
        )
        return

    # Which commands finished — labels captured at spawn (claude: shell id +
    # command; codex: command text). See compose_command_nudge.
    labels = [bgreg.label_for(t) for t in sorted(bgreg.completed)]
    nudge = compose_command_nudge(count, labels)

    # Path 1: WS connected — deliver as a server turn via the notify queue. The
    # per-command badges are already cleared (resolve_bg_command), so unlike the
    # subagent path we don't push a separate "complete" UI frame here.
    notify_queue = _dashboard_notify_queues.get(session_id)
    if notify_queue:
        await notify_queue.put({
            "type": "bg_command_nudge",
            "session_id": session_id,
            "chat_id": chat_id,
            "count": count,
            "labels": labels,
        })
        logger.info(f"BG command monitor: nudge queued for session={session_id[:8]}")
        return

    # Path 2: Pump running (background drain) — queue on pump for in-context delivery.
    task_store.add_chat_message(chat_id, "event", "",
        event_type="bg_command_nudge",
        event_data=json.dumps({"count": count, "labels": labels}))
    if queue_pump_prompt(chat_id, nudge, system=True):
        logger.info(f"BG command monitor: nudge queued on pump for chat={chat_id[:8]}")
        return

    # Path 3: No pump, no WS — send directly via the execution layer.
    logger.info(f"BG command monitor: WS disconnected, delivering directly for session={session_id[:8]}")
    try:
        parts: list[str] = []
        async with layer.session_lock(session_id):
            async for event in layer.send_message(session_id, nudge):
                if event.type == "text":
                    parts.append(event.data.get("content", ""))
        response = "".join(parts)
        if response:
            task_store.add_chat_message(chat_id, "assistant", response)
    except Exception as e:
        logger.error(f"BG command monitor direct delivery failed: {e}", exc_info=True)
