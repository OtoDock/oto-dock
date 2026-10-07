"""Background-work monitors for dashboard chat turns.

After a turn leaves background subagents or bash commands running, these
monitors wait for completion (deterministic SubagentRegistry signal for
subagents; active stdout drain for bash commands) and nudge the LLM to review
the results. Extracted from stream_pump.py; stream_pump re-exports the public
entry points so existing imports keep working.
"""

import asyncio
import functools
import json
import logging
import time
from contextlib import contextmanager

from storage import database as task_store
from storage.pg import run_db
from core.session.session_state import (
    _dashboard_notify_queues,
    push_pump_event,
    queue_pump_prompt,
    mark_bg_agents_completed,
    get_subagent_registry,
)
from core.events import chat_writer
from core.events.bg_command_state import get_bg_command_registry
from ws import wire_events as wire
from core.events.common_events import TEXT

logger = logging.getLogger("claude-proxy")


def _persist_row(chat_id: str, role: str, content: str, *, label: str,
                 event_type: str = "", event_data: str = "") -> asyncio.Future:
    """Queue one chat row on the chat's writer lane (off the loop, in order
    with the chat's other rows)."""
    return chat_writer.submit(chat_id, functools.partial(
        task_store.add_chat_message, chat_id, role, content,
        event_type=event_type, event_data=event_data,
    ), label=label)


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


async def _self_wakes(layer, session_id: str) -> bool:
    """Whether the session's engine reviews finished background work by
    itself (``layer.session_self_wakes``: Claude; Codex never does)."""
    try:
        wakes = getattr(layer, "session_self_wakes", None)
        return wakes is not None and bool(await wakes(session_id))
    except Exception:
        return False


async def _wake_grace_covers(layer, session_id: str) -> bool:
    """True when a captured self-wake turn covers the just-resolved cohort —
    the nudge stands down. Actively drains during the grace (the drain
    records wake brackets — core/events/wake_capture.py): the subagent path
    has no other between-turns stdout reader, so a passive wait could never
    observe the wake. Claude sessions only (``layer.session_self_wakes``);
    codex has no self-wake and skips the grace entirely."""
    if not await _self_wakes(layer, session_id):
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


# How often a wait on background work looks again at a session whose machine
# is in its reconnect grace.
GRACE_POLL_S = 0.5


async def ride_out_grace(layer, session_id: str) -> bool:
    """Whether a session a background wait watches is still usable. A machine
    in its reconnect grace is waited for (the grace bounds it); once it is
    back its process is asked once, since a satellite restarted inside the
    grace gets the held queues back while it no longer runs the session, and
    what the drop buffered is drained once (a completion hook posted during
    it is lost; the stdout frames are not)."""
    if await layer.is_session_alive(session_id):
        return True
    if not layer.is_session_grace_held(session_id):
        return False
    while layer.is_session_grace_held(session_id):
        await asyncio.sleep(GRACE_POLL_S)
    if not await layer.is_session_alive(session_id):
        return False
    if await layer.probe_session_process_dead(session_id):
        return False
    try:
        await layer.drain_bg_commands(session_id, budget=2.0)
    except Exception:
        logger.debug("grace ride-out: drain failed for %s", session_id[:8], exc_info=True)
    logger.info(f"BG wait: session {session_id[:8]} is back after its machine's reconnect")
    return True


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
        review = await _bg_agent_monitor_impl(layer, session_id, chat_id, count)
    finally:
        _bg_monitors_running.discard(session_id)
    if review:
        nudge, agent_ids = review
        await _review_turn(layer, session_id, chat_id, nudge, commands=False,
                           agent_ids=agent_ids)


async def _bg_agent_monitor_impl(
    layer, session_id: str, chat_id: str, count: int,
) -> tuple[str, list[str]] | None:
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
    serialization. A run on the chat or its session that owes its report is
    waited out first; then only the agents no review named yet are owed one.
    Returns the nudge and the agents it names when neither a socket nor a
    consumer pump took it: the wrapper runs that review (``_review_turn``)
    once the monitor's guard is released.

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

        if not await ride_out_grace(layer, session_id):
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
    if not await ride_out_grace(layer, session_id):
        return

    # A run on this chat or session owes its report: its producer reviews the
    # agents it inherits. After that window only what no review named is owed,
    # and an engine that wakes itself gave its own turn the rest.
    waited = await _wait_report_window(layer, session_id, chat_id)
    if waited is None:
        return
    owed = sorted(reg.owed)
    if not owed or (waited and await _engine_self_wakes(chat_id)):
        mark_bg_agents_completed(chat_id)
        push_pump_event(chat_id, {"type": wire.BG_AGENTS_COMPLETE, "count": count})
        logger.info(f"BG agent monitor: no agent review owed (session={session_id[:8]})")
        return

    # The CLI's own self-wake review turn (≥2.1.243) supersedes the nudge —
    # firing ours too would run a second, redundant review at double cost.
    # The cohort's badges still clear (the wake turn was recorded as a real
    # turn; the *_complete cohort frame below keeps reconnect state honest).
    if await _wake_grace_covers(layer, session_id):
        mark_bg_agents_completed(chat_id)
        push_pump_event(chat_id, {"type": wire.BG_AGENTS_COMPLETE, "count": count})
        logger.info(
            f"BG agent monitor: self-wake turn covered the cohort — nudge "
            f"stands down (session={session_id[:8]})"
        )
        return

    # Which agents finished — so the model can tell each completion apart
    # (dogfooding find 2026-08-27: the count-only nudge made an agent read a
    # still-running sibling's output file). Labels were captured at spawn.
    labels = [reg.label_for(t) for t in owed]
    count = len(owed)
    nudge = compose_agent_nudge(count, labels)

    # Update live state (for reconnect accuracy)
    mark_bg_agents_completed(chat_id)

    # Path 1: WS connected — notification queue (full handling with UI event + LLM prompt)
    notify_queue = _dashboard_notify_queues.get(session_id)
    if notify_queue:
        push_pump_event(chat_id, {"type": wire.BG_AGENTS_COMPLETE, "count": count})
        await notify_queue.put({
            "type": wire.NOTIFY_BG_NUDGE,
            "session_id": session_id,
            "chat_id": chat_id,
            "count": count,
            "labels": labels,
        })
        reg.mark_reviewed(owed)
        logger.info(f"BG agent monitor: nudge queued for session={session_id[:8]}")
        return None

    # Path 2: Pump running (background drain) — queue on pump for in-context delivery
    _persist_row(chat_id, "event", "", event_type=wire.PERSISTED_BG_NUDGE,
                 event_data=json.dumps({"count": count, "labels": labels}),
                 label="bg_nudge")
    if queue_pump_prompt(chat_id, nudge, system=True):
        push_pump_event(chat_id, {"type": wire.BG_AGENTS_COMPLETE, "count": count})
        reg.mark_reviewed(owed)
        logger.info(f"BG agent monitor: nudge queued on pump for chat={chat_id[:8]}")
        return None

    # Path 3: no socket, no consumer pump — the wrapper's review turn.
    return nudge, owed


_bg_command_monitors_running: set[str] = set()  # session_ids with an active bg-command monitor


def bg_command_monitor_running(session_id: str) -> bool:
    """True if a _bg_command_monitor is already watching this session's commands."""
    return session_id in _bg_command_monitors_running


def arm_bg_monitors(layer, session_id: str, chat_id: str) -> None:
    """Start the monitor for each cohort a turn left running: background
    subagents and background commands, each only when its registry has
    pending work and no monitor of that kind watches the session (the
    monitors' per-session guards make every caller idempotent). The one
    arming body for every chat turn source: the dashboard's turns, the turns
    the platform drives into a chat, the reconnect pass."""
    if not (session_id and chat_id and layer):
        return
    reg = get_subagent_registry(session_id)
    if reg.has_pending and not bg_monitor_running(session_id):
        asyncio.create_task(_bg_agent_monitor(layer, session_id, chat_id, reg.pending_count))
    bgreg = get_bg_command_registry(session_id)
    if bgreg.has_pending and not bg_command_monitor_running(session_id):
        asyncio.create_task(
            _bg_command_monitor(layer, session_id, chat_id, bgreg.pending_count))


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
        review = await _bg_command_monitor_impl(layer, session_id, chat_id, count)
    finally:
        _bg_command_monitors_running.discard(session_id)
    if review:
        await _review_turn(layer, session_id, chat_id, review, commands=True)


async def _bg_command_monitor_impl(
    layer, session_id: str, chat_id: str, count: int,
) -> str | None:
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
    (the commands may still be running). Returns the nudge when neither a
    socket nor a consumer pump took it, for the wrapper's review turn."""
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
        if not await ride_out_grace(layer, session_id):
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
        # Past the fast phase a still-running job is polled slowly, up to
        # the background-work ceiling: its completion frame lands on this
        # stdout and nobody else reads it between turns, and the idle
        # reaper spares the session only while the registry shows it.
        from core.session import background_leash
        SLOW_POLL = 15.0
        ceiling = background_leash.background_work_ceiling()
        logger.info(
            f"BG command monitor: {bgreg.pending_count} command(s) still "
            f"running after {MAX_WAIT:.0f}s — polling every {SLOW_POLL:.0f}s "
            f"up to the {ceiling:.0f}s ceiling (session={session_id[:8]})"
        )
        while bgreg.has_pending and (time.monotonic() - start) < ceiling:
            if not await ride_out_grace(layer, session_id):
                logger.info(f"BG command monitor: session {session_id[:8]} gone, exiting")
                return
            progressed = await layer.drain_bg_commands(session_id, budget=POLL_INTERVAL)
            if bgreg.has_pending and not progressed:
                await asyncio.sleep(SLOW_POLL)

    if bgreg.has_pending:
        logger.warning(
            f"BG command monitor: {bgreg.pending_count} command(s) still pending "
            f"at the background-work ceiling — giving up (no nudge)"
        )
        return

    if not await ride_out_grace(layer, session_id):
        return

    # A run on this chat or session owes its report: its producer reviews the
    # commands it inherits (pending plus unseen); the checks below then read
    # what is left once that window closes.
    if await _wait_report_window(layer, session_id, chat_id) is None:
        return

    # The user STOPPED this chat's last turn (graceful abort keeps the CLI —
    # and its backgrounded commands — alive, so this monitor now survives an
    # abort): a nudge would auto-run a turn the user just refused. The CLI's
    # own bg tracking hands the results to the model on the next REAL turn.
    if chat_id and ((await run_db(task_store.get_chat, chat_id)) or {}).get("last_turn_aborted"):
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

    # Every completion landed inside a later turn (the turn-start reset keeps
    # a still-running command pending, so this monitor outlives that turn),
    # where the model already read it: only an unseen completion earns a
    # review turn (the task producer's rule).
    if not bgreg.unsurfaced_count:
        logger.info(
            f"BG command monitor: every completion was surfaced in a later "
            f"turn — no nudge (session={session_id[:8]})"
        )
        return

    # Which commands finished — labels captured at spawn (claude: shell id +
    # command; codex: command text). See compose_command_nudge.
    labels = [bgreg.label_for(t) for t in sorted(bgreg.completed)]
    # A later turn's command joined this monitor: the cohort is what the
    # registry resolved, not the count the first turn started with.
    count = max(count, len(labels))
    nudge = compose_command_nudge(count, labels)

    # Path 1: WS connected — deliver as a server turn via the notify queue. The
    # per-command badges are already cleared (resolve_bg_command), so unlike the
    # subagent path we don't push a separate "complete" UI frame here.
    notify_queue = _dashboard_notify_queues.get(session_id)
    if notify_queue:
        await notify_queue.put({
            "type": wire.NOTIFY_BG_COMMAND_NUDGE,
            "session_id": session_id,
            "chat_id": chat_id,
            "count": count,
            "labels": labels,
        })
        logger.info(f"BG command monitor: nudge queued for session={session_id[:8]}")
        return

    # Path 2: Pump running (background drain) — queue on pump for in-context delivery.
    _persist_row(chat_id, "event", "", event_type=wire.PERSISTED_BG_COMMAND_NUDGE,
                 event_data=json.dumps({"count": count, "labels": labels}),
                 label="bg_command_nudge")
    if queue_pump_prompt(chat_id, nudge, system=True):
        logger.info(f"BG command monitor: nudge queued on pump for chat={chat_id[:8]}")
        return None

    # Path 3: no socket, no consumer pump — the wrapper's review turn.
    return nudge


# A review nobody views runs as a chat turn: the rounds a pump holding the
# chat gets, and how long one round waits for that pump to end.
REVIEW_ROUNDS = 3
REVIEW_WAIT_S = 600.0
# How often a review owed during a run's report window looks at the window.
REPORT_POLL_S = 1.0


async def _wait_report_window(layer, session_id: str, chat_id: str) -> bool | None:
    """While a run on the chat or its session owes its report
    (``lanes.report_pending``), a review waits: the run's producer reviews
    what it inherits, and a review inside the window would land in the
    report. False when no window was open, True after waiting one out, None
    when the session is gone after it (or the backstop against a leaked hold
    ran out)."""
    from core.session import background_leash
    from services.scheduler import lanes
    if not lanes.report_pending(chat_id, session_id):
        return False
    logger.info(f"BG review: chat={chat_id[:8]} owes a run's report — the review waits for it")
    deadline = time.monotonic() + background_leash.background_work_ceiling()
    while lanes.report_pending(chat_id, session_id):
        if time.monotonic() >= deadline:
            logger.warning(f"BG review: chat={chat_id[:8]} still owed a report at the "
                           f"background-work ceiling — the review was not run")
            return None
        await asyncio.sleep(REPORT_POLL_S)
    if not await ride_out_grace(layer, session_id):
        return None
    return True


async def _engine_self_wakes(chat_id: str) -> bool:
    """Whether the chat's engine reviews finished background agents by
    itself (``runtime.self_wakes``), read from the engine, not from a session
    this process holds: a session closed meanwhile still answers."""
    from core.session.session_manager import get_layer_capabilities, resolve_execution_path
    chat = await run_db(task_store.get_chat, chat_id) or {}
    path = await run_db(resolve_execution_path, chat.get("agent") or "",
                        chat.get("execution_path") or "")
    caps = get_layer_capabilities(path)
    return bool(caps and caps.runtime.self_wakes)


def _mark_reviewed(session_id: str, agent_ids) -> None:
    if agent_ids:
        get_subagent_registry(session_id).mark_reviewed(agent_ids)


async def _review_turn(layer, session_id: str, chat_id: str, nudge: str, *,
                       commands: bool, agent_ids=()) -> None:
    """Path 3 of both monitors, run after the monitor's guard is released so
    a review that starts more background work gets its own monitor at its
    end. On a dashboard or task chat driving its own session the review is a
    headless chat turn (``_run_echo_turn_pumped``: streamed, persisted,
    unread-stamped, a viewer may attach), never inside a run's report window
    (it waits that out at the top of every round); anywhere else (a meeting
    participant's session against the meeting's chat, a phone chat, a live
    terminal) the direct send. A pump holding the chat takes the nudge when
    it drains system prompts; otherwise it is waited for and the review
    re-checked, a few rounds at most. ``agent_ids``: the finished agents the
    nudge names, marked reviewed once it is delivered."""
    from core.events.stream_pump import _active_pumps
    from core.session import interactive_session
    from services.scheduler import delivery, lanes
    chat = await run_db(task_store.get_chat, chat_id)
    if (not lanes.drives_own_session(chat, session_id)
            or interactive_session.find_live_for_chat(chat_id) is not None):
        await _direct_review(layer, session_id, chat_id, nudge, agent_ids=agent_ids)
        return
    for round_no in range(REVIEW_ROUNDS):
        waited = await _wait_report_window(layer, session_id, chat_id)
        if waited is None:
            return
        if (waited or round_no) and not await _review_still_owed(
                session_id, chat_id, commands=commands, agent_ids=agent_ids):
            logger.info(f"BG review: the turn that held chat={chat_id[:8]} took "
                        f"the completions — no review")
            return
        if await delivery._run_echo_turn_pumped(
                layer, session_id, chat_id, chat.get("agent") or "", nudge,
                only_as_chat_turn=True) is not None:
            _mark_reviewed(session_id, agent_ids)
            return
        if queue_pump_prompt(chat_id, nudge, system=True):
            _mark_reviewed(session_id, agent_ids)
            logger.info(f"BG review: nudge queued on pump for chat={chat_id[:8]}")
            return
        task = getattr(_active_pumps.get(chat_id), "_task", None)
        if task is not None:
            await asyncio.wait([task], timeout=REVIEW_WAIT_S)
    logger.warning(f"BG review: chat={chat_id[:8]} stayed busy for "
                   f"{REVIEW_ROUNDS} rounds — the review was not run")


async def _review_still_owed(session_id: str, chat_id: str, *, commands: bool,
                             agent_ids=()) -> bool:
    """After another turn held the chat, or a run's report window: is the
    review still owed? A turn start's reset clears the commands' unseen
    completions (the turn read them), and a run's producer clears what it
    reviewed. For agents: those a review already named are not owed again
    (``SubagentRegistry.reviewed``), and an engine that wakes itself handed
    finished agents to its own turn, while Codex never tells the parent
    thread, so its review stays owed."""
    if commands:
        return bool(get_bg_command_registry(session_id).unsurfaced_count)
    if agent_ids and not set(agent_ids) & get_subagent_registry(session_id).owed:
        return False
    return not await _engine_self_wakes(chat_id)


async def _direct_review(layer, session_id: str, chat_id: str, nudge: str, *,
                         agent_ids=()) -> None:
    """The review sent straight to the session, outside any pump: the joined
    text is persisted as one assistant row."""
    logger.info(f"BG review: delivering directly for session={session_id[:8]}")
    try:
        parts: list[str] = []
        async with layer.session_lock(session_id):
            async for event in layer.send_message(session_id, nudge):
                if event.type == TEXT:
                    parts.append(event.data.get("content", ""))
        response = "".join(parts)
        _mark_reviewed(session_id, agent_ids)
        if response:
            await _persist_row(chat_id, "assistant", response, label="bg_direct_reply")
    except Exception as e:
        logger.error(f"BG review direct delivery failed: {e}", exc_info=True)
