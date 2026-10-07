"""Lane helpers for task runs: output collection, the limit notice, lane
quiescence and settling, warm-session reuse, the close before a round that
changed the worker's model, the post-run session action, the prior-pump
reaper, and the pump stall watchdog.

One piece of the task scheduler; ``services/scheduler/scheduler.py`` is the
facade that assembles them and holds the registry API. Every ``core.*`` and
service import stays function-local so the standalone scheduler can import
this package without the platform.
"""

import asyncio
import logging
import re
import time
from collections.abc import Awaitable, Callable


import config
from storage import database as task_store
from storage.automation import run_status

logger = logging.getLogger("claude-proxy.scheduler")


def _collect_task_output(chat_id: str, after_id: int = 0) -> str:
    """Collect task output from chat_messages for delivery/storage.

    ``after_id`` is the pre-run message cursor: only rows persisted after it
    are collected, so a run on a chat that already has history (a target-chat
    worker, a multi-round continue) reports THIS round's output instead of
    re-sending every prior round."""
    messages = task_store.get_chat_messages(chat_id)
    parts = []
    for m in messages:
        if after_id and (m.get("id") or 0) <= after_id:
            continue
        if m["role"] == "assistant" and m.get("content"):
            parts.append(m["content"])
    return "\n\n".join(parts)


_DELEGATE_PREVIEW_CAP = 12000


def _delegate_output_preview(output_text: str) -> str:
    """Bound the delegate_result bubble payload (DB row + WS/pump frames).

    Keeps the TAIL on overflow — the lane narration ends with the worker's
    final summary, which is the part the delegating user needs — and says so
    explicitly instead of cutting mid-word."""
    if len(output_text) <= _DELEGATE_PREVIEW_CAP:
        return output_text
    return (
        f"… [output truncated — showing the last {_DELEGATE_PREVIEW_CAP} "
        "chars; open the worker chat for the full run]\n\n"
        + output_text[-_DELEGATE_PREVIEW_CAP:]
    )


def _limit_notice(final_output: str) -> str | None:
    """Detect a provider usage-limit notice ending a run's output.

    The CLIs stream the limit notice as NORMAL result text ("You've hit your
    session limit · resets 3pm", "You've reached your Fable limit. …",
    "You're out of usage credits. …", "Claude AI usage limit reached|<ts>"), so the
    turn ends cleanly and the run would be stamped `completed` with the notice
    as its output. Only the LAST non-empty line is eligible and it must START
    with a known notice shape — a run whose real output merely discusses usage
    limits doesn't match.
    """
    if not final_output:
        return None
    tail = final_output[-300:].replace("’", "'")
    for line in reversed(tail.splitlines()):
        line = line.strip()
        if not line:
            continue
        low = line.lower()
        if ((low.startswith(("you've hit your", "you've reached your")) and "limit" in low)
                or low.startswith("you're out of usage credits")
                or low.startswith("claude ai usage limit reached")
                or low.startswith("usage limit reached")):
            return line
        return None
    return None


_PASTED_CONTENT_TAG_RE = re.compile(r"</?pasted_content\b[^>]*>")


def _prompt_key(text: str) -> str:
    """A user row and the prompt that produced it, made comparable: the
    Claude TUI journals a multi-line paste wrapped in ``<pasted_content>``
    tags, and every terminal re-flows whitespace."""
    return " ".join(_PASTED_CONTENT_TAG_RE.sub("", text or "").split())


def _collect_lane_output_since(chat_id: str, after_id: int = 0,
                               skip_row_id: int = 0, own_prompt: str = "") -> str:
    """Batched delegate-lane collection: everything the lane produced after
    the pre-run cursor — assistant turns verbatim, USER rows labelled
    ``[User interjected]`` so the delegating agent sees redirects/steering in
    order. The run's own driven prompt is excluded: by row id when the caller
    persisted it (headless), by first-content-match otherwise (interactive
    runs, where the transcript tailer backfills it as a user row, possibly
    wrapped as pasted content — see ``_prompt_key``)."""
    own_key = _prompt_key(own_prompt)
    own_prompt_pending = bool(own_key)
    messages = task_store.get_chat_messages(chat_id)
    parts = []
    for m in messages:
        mid = m.get("id") or 0
        if after_id and mid <= after_id:
            continue
        if skip_row_id and mid == skip_row_id:
            continue
        content = m.get("content") or ""
        if m["role"] == "assistant" and content:
            parts.append(content)
        elif m["role"] == "user" and content:
            if own_prompt_pending and _prompt_key(content) == own_key:
                own_prompt_pending = False
                continue
            parts.append(f"[User interjected]: {content}")
    return "\n\n".join(parts)


async def _await_lane_quiescence(chat_id: str, *, ceiling_seconds: float = 1800.0,
                                 settle_seconds: float = 5.0,
                                 immediate_quiet_ok: bool = True) -> None:
    """Wait until no live activity remains on a delegate lane's chat.

    A user may be steering the worker when its driven turn ends — a queued
    pump message, a follow-up turn, or an open interactive turn. Collecting
    mid-activity would deliver a half-conversation, so wait for the lane to
    stay quiet for ``settle_seconds`` (first probe quiet → return at once —
    the common fire-and-forget case pays no latency). ``ceiling_seconds``
    bounds the wait (heartbeat-logged); runs INSIDE the delivery task, never
    under the run's RAM slot.

    ``immediate_quiet_ok=False`` disables the first-probe fast path: an
    interrupted lane is quiet BECAUSE the user just stopped it — their
    redirect (and the worker's reply to it) is what the settle window is
    there to capture."""
    from core.events import input_queue
    from core.events.stream_pump import _active_pumps
    from core.session import interactive_session

    start = time.monotonic()
    last_heartbeat = start
    quiet_since: float | None = None
    while True:
        pump = _active_pumps.get(chat_id)
        # A message waiting in the chat's queue (or a turn starter about to
        # take it) still goes out as the lane's next turn.
        busy = (pump is not None and not pump.is_done) or input_queue.busy(chat_id)
        if not busy:
            live = interactive_session.find_live_for_chat(chat_id)
            busy = live is not None and (live._turn_open or bool(live._prompt_queue))
        now = time.monotonic()
        if busy:
            quiet_since = None
        elif quiet_since is None:
            if immediate_quiet_ok and now - start < 1.0:
                return  # quiet on first probe — nothing ever queued
            quiet_since = now
        elif now - quiet_since >= settle_seconds:
            return
        if now - start >= ceiling_seconds:
            logger.warning(
                f"Lane quiescence ceiling reached for chat {chat_id[:8]} "
                f"({int(ceiling_seconds)}s) — collecting anyway"
            )
            return
        if now - last_heartbeat >= 60.0:
            logger.info(f"Delegate delivery waiting on active lane chat {chat_id[:8]}")
            last_heartbeat = now
        await asyncio.sleep(1.0)


def _lane_pump_wedged(pump) -> bool:
    """Can the prior round's open pump still make progress? Healthy live
    pumps get a quiescence wait instead of a reap — a reap here would cancel
    a user's in-flight turn on the lane (its producer holds
    layer.session_lock; the reap shoots the holder)."""
    sid = pump.session_id
    from core.session.session_manager import _remote_layer
    if _remote_layer is not None and sid in _remote_layer._sessions:
        info = _remote_layer._sessions[sid]
        return _remote_layer.remote_stream_severed(sid) or info.cli_dead
    from core.layers.cli.session import _persistent_sessions
    ps = _persistent_sessions.get(sid)
    if ps is not None:
        return ps.proc is None or ps.proc.returncode is not None
    from core.layers.codex.session import _codex_sessions
    from core.layers.direct.session import _direct_sessions
    if sid in _codex_sessions or sid in _direct_sessions:
        return False
    # No live session backs the pump at all — orphaned producer.
    return True


async def _settle_prior_lane(chat_id: str, run_id: str) -> bool:
    """Run-start lane gate: wait out a HEALTHY prior round / user turn (the
    delivery-side quiescence gate never covered run START — the reap used to
    cancel live turns), then reap only what is genuinely wedged. Returns True
    when a reap happened — the round must then respawn, never reuse: a
    producer-cancel leaves the satellite's per-turn read loop running, so
    reusing would put two concurrent stdout readers on one CLI, while the
    respawn path's stale-live guard closes the old process first."""
    from core.events.stream_pump import _active_pumps
    prior = _active_pumps.get(chat_id)
    if prior is None or prior.is_done:
        return False
    if not _lane_pump_wedged(prior):
        await _await_lane_quiescence(chat_id)
        prior = _active_pumps.get(chat_id)
        if prior is None or prior.is_done:
            return False
    await _reap_prior_lane_pump(chat_id, run_id)
    return True


async def _try_reuse_warm_session(
    layer, agent_cfg, session_id: str, run_id: str,
) -> bool:
    """A2 reuse decision for a delegate continue round: True = drive the turn
    on the already-alive warm REMOTE session instead of respawning.

    Remote-only: local start_session already reuses an alive process and
    re-stamps identity/binding internally, but the remote spawn path commands
    the satellite, whose stale-live guard KILLS the existing CLI (the
    2026-07-20 incident destroyed a healthy worker mid-round this way).
    Every fall-through lands on the respawn path — the correct cleanup for
    dead/reaped/foreign states. Conditions:
    - the resolved layer is the remote layer, it holds the session alive,
      no turn in flight, process not known-dead;
    - the session lives on the machine this run resolved to;
    - the session was task-spawned (is_task): a dashboard-warmed session on
      the lane chat runs under the viewer's context/subscription/env — a
      task round must not ride it."""
    from core.session import session_state as _state
    from core.session.session_manager import _remote_layer
    if _remote_layer is None or layer is not _remote_layer:
        return False
    info = _remote_layer._sessions.get(session_id)
    if info is None or not info.alive or info.cli_dead or info.turn_active:
        return False
    if info.machine_id != (agent_cfg.execution_target or ""):
        return False
    if not (_state._sessions.get(session_id) or {}).get("is_task"):
        return False
    if await _remote_layer.is_session_process_dead(session_id):
        return False
    logger.info(
        f"Task {run_id}: reusing warm remote session {session_id[:8]} "
        f"on {info.machine_id[:8]} (no respawn)"
    )
    return True


async def _close_for_model_change(session_id: str, run_id: str, *, slot_target: str) -> bool:
    """Close a continued lane's live session before a round that changed the
    worker's model (``TaskDefinition.respawn_for_model``), so the round's
    spawn resumes the conversation on the new model: a live local process
    would be reused as it is, and a warm remote one ridden.

    Called after the round's last lane gate, so no turn of the lane is open;
    the close runs under the holding layer's session lock, which a delivery
    wake into the session holds while it sends. Background work the old
    process still ran ends with it. The close releases the admission slot
    under ``session_id``, which is also this round's: it is taken back
    (blocking, as the round's own ``task_slot`` took it) so the new process
    is counted. Returns True when a live session was closed."""
    from core.session.session_manager import find_layer_for_session
    holder = find_layer_for_session(session_id)
    if holder is None:
        return False
    async with holder.session_lock(session_id):
        await holder.close_session(session_id)
    logger.info(
        f"Task {run_id}: closed live session {session_id[:8]} — the worker's "
        f"model changed, the round resumes it on the new one"
    )
    from core import concurrency
    await concurrency.acquire(session_id, "task", target=slot_target, blocking=True)
    return True


def _post_run_session_action(chat_row: dict | None) -> tuple[str, bool]:
    """Map a finished lane turn's abort flags to (final_status, keep_warm).

    A user-stopped lane's session close depends on HOW it stopped. GRACEFUL
    abort (soft interrupt landed, turn closed normally): the CLI/daemon +
    MCP sidecars survived by contract — keep the session warm exactly like a
    dashboard chat, so the next prompt or continue round reuses it (no cold
    re-warm, live model changes apply, still-running subagent children keep
    reporting); the idle reaper collects it if nobody returns. HARD abort:
    the process is gone / the stream is not reliably writable again — close
    outright: the chat row keeps its session_id, so the user's next message
    takes the lazy-warmup path (honoring a model change made while stopped)
    and --resume restores the conversation. Interactive-PTY stops and run
    crashes never stamp the graceful flag, so they always close."""
    row = chat_row or {}
    if not row.get("last_turn_aborted"):
        return run_status.COMPLETED, False
    return run_status.USER_INTERRUPTED, bool(row.get("last_abort_graceful"))


async def _reap_prior_lane_pump(chat_id: str, run_id: str) -> None:
    """Persist-and-clear a PRIOR round's pump before a new round fires on a
    continued lane (delegate ``continue_id`` / any run on ``target_chat_id``).

    A previous turn's pump can still be open here — typically a wedged one
    whose turn-end never arrived (severed satellite event stream). Registering
    the new round's pump would orphan it SILENTLY with its unflushed turn
    blocks, erasing that round's transcript from chat_messages (the dashboard
    then shows only the new round). Abort it and await its teardown — the
    pump's ``finally`` persists the partial turn — so the prior transcript is
    durably in the DB before this round's output cursor and prompt land."""
    from core.events.stream_pump import _active_pumps
    prior = _active_pumps.get(chat_id)
    if prior is None or prior.is_done:
        return
    logger.warning(
        f"Task {run_id}: prior round's pump still open on lane chat "
        f"{chat_id[:8]} (session={prior.session_id[:8]}) — reaping so its "
        f"turn persists before the new round"
    )
    prior.abort()
    if prior._task is not None:
        # Wait WITHOUT cancelling — the pump's finally flushes the partial
        # turn; a laggard past the timeout is left to finish on its own.
        await asyncio.wait([prior._task], timeout=5.0)
    # The pump's rows are writer-lane jobs: wait for them to land before
    # this round reads its output cursor, or the prior round's tail would be
    # collected as this run's output.
    from core.events import chat_writer
    await chat_writer.drain(chat_id, timeout=5.0)


# Mirrors ws/dashboard.py's STALE_TURN_SECS (event silence that ARMS a
# liveness probe — not death by itself; see the Mode D incident note there).
_STALL_PROBE_SECS = 90.0
_WATCHDOG_SLICE_S = 60.0


class _TaskTurnStalled(RuntimeError):
    """A headless task turn was reaped by the stall watchdog."""


async def _watch_task_pump(layer, pump, run_id: str, chat_id: str,
                           session_id: str, *,
                           on_parked: Callable[[], Awaitable[None]] | None = None) -> None:
    """Await the task pump with a stall watchdog. ``on_parked`` is awaited
    once, the first time the turn waits on a person (a permission card, a
    question) while no viewer is attached to the pump.

    A headless turn had no wall-clock backstop: ``await pump._task`` waits for
    PRODUCER_DONE forever, and the wedge reap in ws/dashboard_chat.py only
    runs when a user re-opens the chat — an unwatched lane could sit
    "generating" for hours (observed: a lane stuck 1.5h+ in a never-ending
    tool call). Same reap criteria as the dashboard's: a severed remote
    stream, a silent-AND-dead process, or silence past the CLI turn ceiling.
    An alive process below the ceiling always gets its leash.
    """
    from core.session import session_state
    told = on_parked is None
    while True:
        try:
            await asyncio.wait_for(asyncio.shield(pump._task),
                                   timeout=_WATCHDOG_SLICE_S)
            return
        except asyncio.TimeoutError:
            pass
        if (not told and session_state.has_pending_prompt(session_id)
                and not pump.has_viewers):
            told = True
            try:
                await on_parked()
            except Exception:
                logger.warning("Task watchdog: the parked-prompt notice for chat %s failed",
                               chat_id, exc_info=True)
        if pump.producer.done():
            continue  # turn tail persisting — the pump is about to exit
        severed = layer.remote_stream_severed(session_id)
        idle = layer.session_idle_seconds(session_id)
        stale = idle is not None and idle > _STALL_PROBE_SECS
        # A prompt waiting on a person is silent by design; its own wait
        # bounds it.
        hard_stale = (idle is not None and idle > config.CLAUDE_TIMEOUT
                      and not session_state.has_pending_prompt(session_id))
        proc_dead = False
        if stale and not severed and not hard_stale:
            proc_dead = await layer.probe_session_process_dead(session_id)
        if not (severed or (stale and proc_dead) or hard_stale):
            continue
        if severed:
            reason = "stream severed by a satellite reconnect"
        elif hard_stale:
            reason = (f"stalled: no stream events for {int(idle)}s, "
                      f"hard ceiling exceeded")
        else:
            reason = (f"stalled: no stream events for {int(idle or 0)}s, "
                      f"process dead")
        logger.warning(
            f"Task watchdog: reaping stalled turn run={run_id} chat={chat_id} "
            f"session={session_id[:8]} ({reason})"
        )
        # The lost ending first: the run's record and a viewer get a
        # reason, then the producer's cancellation follows it on the queue.
        from core.events import turn_ending
        from core.events.common_events import CommonEvent, ERROR
        _ending = turn_ending.TurnEnding(reason=turn_ending.LOST, detail=reason)
        pump.event_queue.put_nowait(CommonEvent(type=ERROR, data={
            "message": _ending.line(), "ending": _ending.as_dict()}))
        pump.abort()
        if pump._task is not None:
            # Wait WITHOUT cancelling — the pump's finally persists the
            # partial turn; a laggard is simply left to finish.
            await asyncio.wait([pump._task], timeout=2.0)
        # ...and its rows are writer-lane jobs — let them land so the failure
        # record collects the partial output.
        from core.events import chat_writer
        await chat_writer.drain(chat_id, timeout=2.0)
        try:
            await layer.prepare_resume(session_id)
        except Exception:
            logger.exception(
                f"Task watchdog: prepare_resume failed run={run_id}"
            )
        raise _TaskTurnStalled(f"reaped by platform: {reason}")


# A run owes its report from its start until its output is collected
# (``delivery._finalize_and_deliver``): keyed by the run's chat and by its
# session (a sibling chat can name the same session), counted because a
# continue round can start while the previous run's collection still waits.
_report_holds: dict[str, int] = {}


def hold_report(*keys: str) -> None:
    for key in keys:
        if key:
            _report_holds[key] = _report_holds.get(key, 0) + 1


def release_report(*keys: str) -> None:
    for key in keys:
        n = _report_holds.get(key, 0) - 1
        if n > 0:
            _report_holds[key] = n
        else:
            _report_holds.pop(key, None)


def report_pending(chat_id: str, session_id: str) -> bool:
    return bool((chat_id and chat_id in _report_holds)
                or (session_id and session_id in _report_holds))


def drives_own_session(chat: dict | None, session_id: str) -> bool:
    """Whether ``chat`` is a dashboard or task chat whose row names
    ``session_id``: a turn on it can run as the chat's own (a phone chat,
    and a session the chat no longer runs, a meeting participant's on the
    meeting's chat among them, cannot)."""
    from core.session import session_kind
    from core.session.visibility import is_phone_chat_owner
    if not chat or not session_id or chat.get("session_id") != session_id:
        return False
    if session_kind.of_chat(chat) not in (session_kind.DASHBOARD, session_kind.TASK):
        return False
    return not is_phone_chat_owner(chat.get("user_sub") or "")


def takes_chat_contract(chat: dict | None, session_id: str) -> bool:
    """Whether a turn the platform drives into ``chat`` on ``session_id`` runs
    as the chat's own turns do (it ends at the engine's result; background
    work it leaves running is the chat's monitors') rather than as a task run
    (it returns only after its background work, so a report carries it): a
    chat driving its own session, a worker's or a task run's included, while
    no run on it owes a report. A phone chat, a session the chat no longer
    runs (a meeting participant's among them), and any chat inside a report
    window keep the task run's producer."""
    return (drives_own_session(chat, session_id)
            and not report_pending((chat or {}).get("id") or "", session_id))


async def _chat_turn_produce(layer, session_id: str, prompt: str,
                             event_queue: asyncio.Queue) -> None:
    """The producer of a turn the platform drives into an ordinary chat: one
    send on the chat's own contract (no settle, no review loop, no hold on
    the chat's monitors), every event to the pump, a failure as the turn's
    ERROR, PRODUCER_DONE last. The caller arms the monitors once the pump
    has ended."""
    from core.events.common_events import CommonEvent, ERROR, PRODUCER_DONE
    try:
        async with layer.session_lock(session_id):
            async for event in layer.send_message(session_id, prompt):
                await event_queue.put(event)
    except Exception as e:
        logger.error(f"Chat turn producer error: {e}", exc_info=True)
        await event_queue.put(CommonEvent(type=ERROR, data={"message": str(e)}))
    finally:
        await event_queue.put(CommonEvent(type=PRODUCER_DONE, data={}))
