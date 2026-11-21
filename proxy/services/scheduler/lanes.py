"""Lane helpers for task runs: output collection, the limit notice, lane
quiescence and settling, warm-session reuse, the close before a round that
changed the worker's model, the post-run session action, the prior-pump
reaper, the pump stall watchdog, the report window, and the turns the
platform drives into a chat (``_chat_turn_produce``, ``_run_echo_turn_pumped``).

One piece of the task scheduler; ``services/scheduler/scheduler.py`` is the
facade that assembles them and holds the registry API. Every ``core.*`` and
service import stays function-local so the standalone scheduler can import
this package without the platform.
"""

import asyncio
import contextlib
import json
import logging
import re
import time
from collections.abc import Awaitable, Callable
from typing import NamedTuple, cast

import config
from core import placement
from storage import database as task_store
from storage.agents import agent_store
from storage.automation import run_status

logger = logging.getLogger("otodock.scheduler")


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
    "You're out of usage credits. …", "Your org is out of usage · …", "Claude AI
    usage limit reached|<ts>"), so the turn ends cleanly and the run would be
    stamped `completed` with the notice as its output. Only the LAST non-empty
    line is eligible and it must START with a known notice shape — a run whose
    real output merely discusses usage limits doesn't match.
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
                or low.startswith(("you're out of usage", "your org is out of usage"))
                or low.startswith("claude ai usage limit reached")
                or low.startswith("usage limit reached")):
            return line
        return None
    return None


def _cut_short_phrases() -> dict[str, str]:
    """The worker's own end before its report, by its ``turn_ending`` word,
    as the delegating agent's callback says it. A person's Stop is not one
    (its run reads ``user_interrupted``)."""
    from core.events import turn_ending as te
    return {
        te.DECLINED: "the model declined to continue",
        te.LIMIT: "the usage limit was reached",
        te.ERROR: "the engine reported an error",
        te.EXITED: "the program stopped",
        te.SILENT: "it went silent and was ended",
        te.LOST: "the machine's connection was lost",
        te.RESTARTED: "the platform restarted",
    }


# The report's own line for what the caller must do (the delegation MCP asks
# every worker for it): plain, bulleted, or in bold.
_BLOCKED_ON_RE = re.compile(r"^\s*(?:[-*+>]\s+)?[*_]*\s*blocked on\s*[*_]*\s*:", re.IGNORECASE | re.MULTILINE)
# How ``_collect_lane_output_since`` labels a person's message in the lane.
_INTERJECTED = "[User interjected]:"


def report_missing(cut_short: str, status: str, output: str,
                   files: list | None, files_skipped: list | None,
                   *, continued: bool = False) -> bool:
    """Whether a delegate result ended without a report: the run's own end
    cut the worker's turn short (``cut_short``, a ``turn_ending`` word the
    runner decides from the session, never from the answer's length), the
    run is not a person's Stop or a cancellation, the worker attached no
    file and wrote no ``Blocked on:`` line, and nobody continued the lane
    after its turn ended (``continued``: a person's message written after
    the run's turn, ``user_rows_after``; a steer during the turn does not
    count, the turn was still cut)."""
    if cut_short not in _cut_short_phrases():
        return False
    if status in (run_status.USER_INTERRUPTED, run_status.CANCELLED):
        return False
    if files or files_skipped or continued:
        return False
    # The worker's own words: a person's interjected line is not its report.
    own = "\n\n".join(part for part in (output or "").split("\n\n")
                       if not part.startswith(_INTERJECTED))
    return _BLOCKED_ON_RE.search(own) is None


# The badges the platform stamps on the user rows it writes into a lane (the
# dashboard draws them as cards): a delegated task or steer names the agent
# it came from, a task run with no delegating agent names its own.
DELEGATED_BY_BADGE = "delegated by"
TASK_PROMPT_BADGE = "task prompt"


def prompt_row_meta(delegating_slug: str | None, executing_slug: str) -> dict:
    """The ``event_data`` of a prompt row the platform writes: the agent it
    came from (the delegating one, else the executing one) with its name
    and colour as the dashboard's header draws them, and the badge."""
    slug = delegating_slug or executing_slug
    data = agent_store.get_agent(slug) or {}
    return {
        "agent_slug": slug,
        "agent_display_name": data.get("display_name") or slug,
        "agent_color": data.get("color", ""),
        "badge": DELEGATED_BY_BADGE if delegating_slug else TASK_PROMPT_BADGE,
    }


def _platform_prompt(m: dict) -> bool:
    """A user row the platform wrote (a delegated task or steer, a task run's
    prompt, a meeting prompt): its meta names the agent it came from."""
    data = m.get("event_data")
    if not data:
        return False
    try:
        return bool(json.loads(data).get("agent_slug"))
    except (TypeError, ValueError, AttributeError):
        return False


def _by_person(m: dict) -> bool:
    return m["role"] == "user" and not _platform_prompt(m)


def user_rows_after(chat_id: str, cursor: int) -> bool:
    """Whether a person wrote in the worker chat after ``cursor`` (the
    chat's last row when the run's turn ended)."""
    if not cursor:
        return False
    return any(_by_person(m) and (m.get("id") or 0) > cursor
               for m in task_store.get_chat_messages(chat_id))


def missing_report_note(cut_short: str, worker_chat_id: str) -> str:
    """The first line of a report-less result's callback: what ended the
    worker's turn and how to get the report, so the delegating agent does not
    read the worker's last message as the outcome."""
    why = _cut_short_phrases().get(cut_short, "its turn ended early")
    cont = (f'delegate(continue_id="{worker_chat_id}")' if worker_chat_id
            else "delegate(continue_id=…)")
    return (f"The worker ended without a report: {why}. Its last message "
            f"below is not the outcome. Continue the lane with {cont} to get "
            f"the report.")


def _collect_lane_output_since(chat_id: str, after_id: int = 0,
                               skip_row_id: int = 0, own_prompt: str = "") -> str:
    """Batched delegate-lane collection: everything the lane produced after
    the pre-run cursor — assistant turns verbatim, a PERSON's rows labelled
    ``[User interjected]`` so the delegating agent sees redirects/steering in
    order (the platform's own rows, a delegated steer's included, are not a
    person's). The run's own driven prompt is excluded: by row id when the
    run wrote it, by first-content-match otherwise (a lane from before the
    runs wrote their rows, where the transcript tailer backfilled it as a
    plain user row, possibly wrapped as pasted content and stamped — see
    ``transcript_tool_events.prompt_key``)."""
    from core.session.transcript_tool_events import prompt_key
    own_key = prompt_key(own_prompt)
    own_prompt_pending = bool(own_key) and not skip_row_id
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
        elif _by_person(m) and content:
            if own_prompt_pending and prompt_key(content) == own_key:
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
                "Lane quiescence ceiling reached for chat %s "
                "(%ss) — collecting anyway", chat_id[:8], int(ceiling_seconds)
            )
            return
        if now - last_heartbeat >= 60.0:
            logger.info("Delegate delivery waiting on active lane chat %s", chat_id[:8])
            last_heartbeat = now
        await asyncio.sleep(1.0)


# Why a round on a lane's chat still waits, from the store (``_lane_waits``).
LANE_WAITS_CUT = "cut"            # a cut turn of the chat nobody settled
LANE_WAITS_EARLIER = "earlier"    # an earlier follow-up of the lane is still live
_LANE_POLL_S = 2.0
# A follow-up never starts a session beside a turn its machine may still
# hold: at the gate's ceiling it ends with these words instead.
LANE_MACHINE_AWAY = ("The worker's machine has not come back, so this follow-up was not "
                     "started. Send it again with {call} once the machine is back.")
LANE_NOT_TAKEN_UP = ("The lane's last turn was cut and has not been taken up again, so this "
                     "follow-up was not started. Send it again with {call} in a while.")


def lane_words(words: str, chat_id: str) -> str:
    """The gate's sentence with the call that sends the follow-up again."""
    return words.format(call=f'delegate(continue_id="{chat_id}")')


class LaneNotStarted(RuntimeError):
    """The lane gate ended a follow-up before its round opened: nothing of
    it ran. The run's report says "was not started", never "failed"."""


class LaneWait(NamedTuple):
    """Why a round still waits at the lane gate (``why``: "" for nothing),
    the words that end it at the gate's ceiling, whether somebody holds the
    cut turn (``held``: the continue pass claimed its record, or an adoption
    took it for its replay), and whether an earlier follow-up of the lane is
    still in line (``behind``: live, its round not opened)."""
    why: str = ""
    words: str = ""
    held: bool = False
    behind: bool = False


def _lane_waits(chat_id: str, run_id: str) -> LaneWait:
    """Why a round of ``run_id`` on a lane's chat must still wait: a cut turn
    of the chat nobody settled, or an earlier follow-up of the lane still
    live (rounds run in the order they were delegated). The run's own record
    never holds it (a reopened run).

    A cut turn's record is unsettled in four states, and a session started
    beside any of them would end the turn's process or run beside it:

    * ``waiting`` and parked: its machine has not reported. Nothing runs here.
    * ``waiting`` and claimed: an adoption replays the turn. Its pump runs on
      the chat from a moment after the claim to a moment before the record
      settles or goes back to the pass.
    * ``pending``: the continue pass has not taken it yet.
    * ``running``: the pass holds it. It starts the chat's session, then the
      continued turn's pump runs on the chat until the record settles.

    ``held`` is true only while a holder of this process has the record
    (``cut_turns.held``): it moves the record on at its end. A ``waiting``
    or ``running`` record whose holder is gone (a process that died) is held
    by nobody, so the gate does not wait twice the ceiling on it.

    So an unsettled record does not mean nobody took the turn up: the gate
    reads the chat's pump for that (``_await_lane_turn``). An earlier
    follow-up with no session on its row has not opened its round: it is in
    line ahead of this one (``behind``), where one whose round is open is
    the lane's running turn. Synchronous: the DB executor."""
    from services.scheduler import cut_turns
    from storage.chat import cut_records
    rounds = task_store.live_delegate_rounds(chat_id)
    ahead = [r["id"] for r in rounds]
    earlier = rounds[:ahead.index(run_id)] if run_id in ahead else []
    behind = any(not (r.get("session_id") or "") for r in earlier)
    record = cut_records.unsettled_of(chat_id)
    if record is not None and (record.get("run_id") or "") != run_id:
        state, sid = record["state"], record.get("session_id") or ""
        held = state in (cut_records.WAITING, cut_records.RUNNING) and cut_turns.held(record["id"])
        # Parked: its machine has not come back. Any other: it did, or the
        # platform's own turn waits for the pass.
        parked = state == cut_records.WAITING and bool(sid) and cut_turns.waiting(sid)
        words = LANE_MACHINE_AWAY if parked else LANE_NOT_TAKEN_UP
        return LaneWait(LANE_WAITS_CUT, lane_words(words, chat_id), held=held, behind=behind)
    if earlier:
        return LaneWait(LANE_WAITS_EARLIER, behind=behind)
    return LaneWait()


def _lane_turn_runs(chat_id: str) -> bool:
    """A turn runs on the lane's chat now (its pump is open): a taken-back
    turn among them (a replay's, the continue pass's)."""
    from core.events.stream_pump import _active_pumps
    pump = _active_pumps.get(chat_id)
    return pump is not None and not pump.is_done


async def _await_lane_turn(chat_id: str, run_id: str, *,
                           ceiling_seconds: float | None = None) -> None:
    """Wait while the store says the lane is not this round's yet
    (``_lane_waits``). The ceiling counts from when the lane began to wait
    for its present reason, so a follow-up that waited out a cut turn does
    not pass the one ahead of it the moment the turn's record settles. The
    rules for a lane with no turn count quiet time on top: from the last
    moment a turn ran on the chat, so nothing is passed or ended in the
    moment after a turn's end, while the one ahead starts its session. What
    the ceiling does, read at every poll past it:

    * A turn runs on the chat (a replay, the pass's continue, a person's own
      turn, an earlier follow-up's round) and no earlier follow-up is in
      line: the gate returns and the live-turn wait that follows waits that
      turn out, as without a restart. A follow-up is never ended behind a
      turn that runs.
    * A turn runs and an earlier follow-up is still in line: the gate waits
      on. The one ahead takes the turn's end first, so two follow-ups never
      leave the gate for one turn's end and start side by side.
    * No turn runs and an earlier follow-up is in line with no cut turn
      unsettled (it waits for its slot, or is a moment before its round):
      the gate waits on, once more the ceiling at most, then passes it. An
      earlier follow-up whose round is open with no turn on the chat is
      passed once the lane has been quiet for the ceiling: its session is
      starting.
    * A cut turn a holder of this process holds with no turn on the chat
      yet (the pass is starting it, an adoption is between its claim and its
      pump): the gate waits on. The holder settles it or hands it back at its
      end, so this is waited for once more the ceiling at most. A holder that
      is gone holds nothing.
    * A cut turn nobody holds and no turn on the chat (its machine has not
      come back, the pass has not taken it): the follow-up ends with its
      words (``LaneNotStarted``). A session started beside that turn would
      end its process.

    A store that cannot be read holds nothing."""
    import psycopg

    from storage.pg import run_db
    ceiling = (float(config.LANE_GATE_CEILING_S) if ceiling_seconds is None
               else ceiling_seconds)
    since = ran_at = last_heartbeat = time.monotonic()
    why = ""
    while True:
        try:
            wait = await run_db(_lane_waits, chat_id, run_id)
        except psycopg.Error:
            logger.debug("Lane gate: the store could not be read for chat %s", chat_id[:8],
                         exc_info=True)
            return
        if not wait.why:
            return
        # Read after the store's answer, with no await between it and the
        # verdict: a pump that registered during the read is seen.
        runs = _lane_turn_runs(chat_id)
        now = time.monotonic()
        if wait.why != why:
            since, why = now, wait.why
        if runs:
            ran_at = now
        waited = now - since
        # How long the lane has had no turn, for the rules that need none.
        quiet = now - max(since, ran_at)
        if waited >= ceiling and (runs or quiet >= ceiling):
            stuck = quiet >= 2 * ceiling
            if runs:
                if not wait.behind:
                    logger.warning("Lane gate ceiling reached for chat %s (%ss): a turn runs "
                                   "on the lane, waiting it out", chat_id[:8], int(ceiling))
                    return
            elif wait.why == LANE_WAITS_EARLIER:
                if not wait.behind or stuck:
                    logger.warning("Lane gate ceiling reached for chat %s (%ss): an earlier "
                                   "follow-up is still live, going on", chat_id[:8], int(ceiling))
                    return
            elif not wait.held or stuck:
                raise LaneNotStarted(wait.words)
        if now - last_heartbeat >= 60.0:
            logger.info("Task %s waits at the lane gate of chat %s (%s%s%s)", run_id, chat_id[:8],
                        wait.why, ", a turn runs" if runs else ", held" if wait.held else "",
                        ", behind an earlier follow-up" if wait.behind else "")
            last_heartbeat = now
        await asyncio.sleep(_LANE_POLL_S)


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
    from core.layers.direct.session import _direct_sessions
    if sid in _direct_sessions:
        return False
    # No live session backs the pump at all — orphaned producer.
    return True


async def _settle_prior_lane(chat_id: str, run_id: str) -> bool:
    """Run-start lane gate: wait for the lane to be this round's by the
    store (``_await_lane_turn``: a cut turn nobody settled, an earlier
    follow-up; a turn that runs is never a reason to end the follow-up
    there), then wait out a HEALTHY prior round / user turn (the
    delivery-side quiescence gate never covered run START — the reap used to
    cancel live turns), then reap only what is genuinely wedged. Returns True
    when a reap happened — the round must then respawn, never reuse: a
    producer-cancel leaves the satellite's per-turn read loop running, so
    reusing would put two concurrent stdout readers on one CLI, while the
    respawn path's stale-live guard closes the old process first."""
    from core.events.stream_pump import _active_pumps
    await _await_lane_turn(chat_id, run_id)
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
      task round must not ride it;
    - the session is bound to the round's payer (``spender.serves``): a lane
      continued by another person pays for that round, on their account."""
    from core.config import spender
    from core.session import session_state as _state
    from core.session.session_manager import _remote_layer
    if _remote_layer is None or layer is not _remote_layer:
        return False
    info = _remote_layer._sessions.get(session_id)
    if info is None or not info.alive or info.cli_dead or info.turn_active:
        return False
    if not placement.runs_on(agent_cfg.execution_target or placement.LOCAL, info.machine_id,
                             platform_machine=info.compute):
        return False
    if not (_state._sessions.get(session_id) or {}).get("is_task"):
        return False
    payer = spender.Spender(getattr(agent_cfg, "subscription_user_sub", "") or None, "round")
    if not await asyncio.to_thread(spender.serves, session_id, payer):
        return False
    if await _remote_layer.is_session_process_dead(session_id):
        return False
    logger.info(
        "Task %s: reusing warm remote session %s "
        "on %s (no respawn)", run_id, session_id[:8], info.machine_id[:8]
    )
    return True


async def _close_for_model_change(session_id: str, run_id: str, *, slot_target: str,
                                  reason: str = "model-change") -> bool:
    """Close a continued lane's live session before a round that changed the
    worker's model (``TaskDefinition.respawn_for_model``), or its payer
    (``reason`` ``payer-change``: a lane continued by another person), so
    the round's spawn resumes the conversation on the new model or account:
    a live local process would be reused as it is, and a warm remote one
    ridden.

    Called after the round's last lane gate, so no turn of the lane is open;
    the close runs under the holding layer's session lock, which a delivery
    wake into the session holds while it sends. Background work the old
    process still ran ends with it. The close releases the admission slot
    under ``session_id``, which is also this round's: it is taken back
    (blocking, as the round's own ``task_slot`` took it) so the new process
    is counted. Returns True when a live session was closed."""
    from core.session import chat_rebuild
    from core.session.session_manager import find_layer_for_session
    held = find_layer_for_session(session_id) is not None
    # Inside the run, so never an idle check: the round's last lane gate
    # passed, and the lock is what a delivery into the session holds. The
    # layer that holds the session closes it, whatever the round's target.
    closed = await chat_rebuild.close_chat_session(
        {"session_id": session_id}, reason=reason, require_idle=False, lock=True)
    if not closed:
        if held:
            # Its machine is away: nothing is sent there, and the machine
            # closes the process it still tracks before it starts the
            # round's under the same id.
            logger.info(
                "Task %s: session %s is on a machine that is away, nothing closed: "
                "the worker's model changed, the machine ends the old process when "
                "the round starts there", run_id, session_id[:8]
            )
        return False
    logger.info(
        "Task %s: closed live session %s (%s), the round resumes it",
        run_id, session_id[:8], reason
    )
    from core import concurrency
    await concurrency.acquire(session_id, "task", target=slot_target, blocking=True)
    return True


def serves_another_payer(session_id: str, task) -> bool:
    """Whether a continue round's live session is bound to another payer
    than the round's (``core/config/spender.py``): another person
    continued the lane. A session outside the pool, or none, is never
    another payer's. Synchronous: the DB executor."""
    from core.config import spender
    return bool(session_id) and not spender.serves(session_id, spender.run_spender(task))


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
        "Task %s: prior round's pump still open on lane chat "
        "%s (session=%s) — reaping so its "
        "turn persists before the new round", run_id, chat_id[:8], prior.session_id[:8]
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
                await cast("Callable[[], Awaitable[None]]", on_parked)()  # told starts True when it is None
            except Exception:  # noqa: BLE001  # the parked-prompt notice is the caller's callback, the watchdog goes on
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
            "Task watchdog: reaping stalled turn run=%s chat=%s "
            "session=%s (%s)", run_id, chat_id, session_id[:8], reason
        )
        # The lost ending first: the run's record and a viewer get a
        # reason, then the producer's cancellation follows it on the queue.
        from core.events import turn_ending
        from core.events.common_events import ERROR, CommonEvent
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
                "Task watchdog: prepare_resume failed run=%s", run_id
            )
        raise _TaskTurnStalled(f"reaped by platform: {reason}")


# A run owes its report from its start until its output is collected
# (``delivery._finalize_and_deliver``): keyed by the run's chat and by its
# session (a sibling chat can name the same session), counted because a
# continue round can start while the previous run's collection still waits.
_report_holds: dict[str, int] = {}


async def tell_run_parked(task, chat_id: str) -> None:
    """A run's turn waits on a person's answer while nobody views its chat:
    the people the drive gate lets answer it are told once (the creator of a
    user-scope task, else who hears the agent's own work: its editors and
    managers and every admin)."""
    from core.session.visibility import SCOPE_USER, agent_work_audience
    from services.notifications import notification_manager
    from storage.pg import run_db

    def _people() -> list[str]:
        if task.scope == SCOPE_USER and task.created_by:
            return [task.created_by]
        return agent_work_audience(task.agent)

    who = "A delegated worker" if task.on_complete_agent else "A task run"
    for sub in await run_db(_people):
        await notification_manager.fire_notification(
            f"{who} waits on your answer",
            f"{task.agent} asked for permission in \"{task.name}\" and waits for an "
            f"answer in its chat. Open the chat to answer.",
            severity="info", scope="user", target=sub, source="task",
            source_id=task.id, agent_slug=task.agent, chat_id=chat_id,
        )


async def tell_chat_waits(agent: str, chat_id: str, person: str,
                          chat_row: dict | None) -> None:
    """A woken turn of a chat waits on its person's answer while nobody
    views it: they are told once. ``person`` is who the turn runs as, else
    the chat's own owner; a chat no person owns tells nobody."""
    from core.session.visibility import is_synthetic_owner
    who = person
    if not who:
        owner = (chat_row or {}).get("user_sub") or ""
        who = "" if is_synthetic_owner(owner) else owner
    if not who:
        return
    from services.notifications import notification_manager
    await notification_manager.fire_notification(
        "A chat waits on your answer",
        f"{agent} was woken in a chat (a delegated result, a scheduled wake, a "
        f"finished background job or a message sent with an API key) and its next "
        f"step waits for your answer there. Open the chat to answer.",
        severity="info", scope="user", target=who, source="delegation",
        agent_slug=agent, chat_id=chat_id,
    )


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
    from core.events.common_events import ERROR, PRODUCER_DONE, CommonEvent
    try:
        async with layer.session_lock(session_id):
            async for event in layer.send_message(session_id, prompt):
                await event_queue.put(event)
    except Exception as e:
        logger.exception("Chat turn producer error: %s", e)
        await event_queue.put(CommonEvent(type=ERROR, data={"message": str(e)}))
    finally:
        await event_queue.put(CommonEvent(type=PRODUCER_DONE, data={}))


async def _run_echo_turn_pumped(layer, session_id: str, chat_id: str,
                                agent: str, result_prompt: str, *,
                                person: str = "", only_as_chat_turn: bool = False,
                                claim: object | None = None, source: str = "") -> str | None:
    """Run a delegate-echo turn through a headless ChatStreamPump on the
    delegating chat.

    The old direct collection (``layer.send_message`` → join TEXT parts) ran
    the turn INVISIBLY: no ``chat_status`` streaming/ready broadcasts, no live
    pump a viewer could attach to, no ``last_response_at`` stamp (so no unread
    dot or Active-now row), and every non-text block dropped — a delivered
    result nobody was told about. The
    pump is the one turn pipeline that does all of that, and a viewer opening
    the chat mid-turn attaches to it like any streaming chat.

    Returns "" on success — the pump persisted the turn itself, so the caller
    must NOT save an echo row — or None when a pump can't run here (the caller
    falls through / logs the refusal). A stalled turn is reaped by the task
    watchdog with its partial output persisted; that still counts as delivered
    (the delegate_result event row is the source of truth either way).

    A turn on a live process runs in the chat's own permission mode: when
    its next step waits on a person while nobody views the chat, ``person``
    (the delivery's, else the chat's owner when a person owns it) is told
    once, so the card does not wait unseen.

    The turn ends at the engine's result, as the chat's own turns do, and the
    chat's monitors take what it leaves running (``takes_chat_contract``);
    only inside a run's report window, or in a phone chat, does it wait on its
    background work as a task turn.

    ``claim``: the chat's claim the caller already holds (the fresh-spawn
    rung prepares the chat under it); released once the pump registers.
    ``source``: what started the turn, as ``chat_rebuild.rebuild_and_resume``
    names it.
    """
    from core.events.stream_pump import ChatStreamPump, _active_pumps
    from core.events.task_producer import task_produce
    from core.session import visibility as _vis
    from core.session.session_state import get_permission_queue
    from storage.pg import run_db

    # Read before the pump check: nothing may await between it and the
    # pump's registration.
    chat_row = await run_db(task_store.get_chat, chat_id)
    from core.events import input_queue
    q = input_queue.get(chat_id)
    if _active_pumps.get(chat_id) is not None or (q.claimed and not q.holds(claim)):
        # A turn started on the chat between the ladder's pump check and now,
        # or a send, a drain or a queued delivery holds the chat for the turn
        # it is starting (its pump registers a few steps later): never
        # dual-pump a chat. The caller keeps the prompt (a stored wake, a
        # review's next round).
        return None
    chat_turn = takes_chat_contract(chat_row, session_id)
    if only_as_chat_turn and not chat_turn:
        return None   # a review never runs inside a run's report
    echo_id = f"echo-{chat_id[:8]}"
    event_queue: asyncio.Queue = asyncio.Queue()
    producer = asyncio.create_task(
        _chat_turn_produce(layer, session_id, result_prompt, event_queue)
        if chat_turn else task_produce(
            layer, session_id, result_prompt, event_queue, echo_id, settle_timeout=30.0))
    pump = ChatStreamPump(
        chat_id=chat_id,
        session_id=session_id,
        producer=producer,
        event_queue=event_queue,
        perm_queue=get_permission_queue(session_id),
        scope="agent" if _vis.is_shared_only(agent) else "user",
        driver_tells_parked=True,   # _tell_parked below
    )
    # Who the platform runs this turn for and what started it (a delegated
    # result, a wake): nobody watches it, so a usage limit that cuts it asks
    # with these whether it goes on (``pool_limits.after_limit``). A caller
    # that names no source (a review, a monitor) keeps the limit's card.
    pump.driven_for, pump.driven_by = person or "", source
    _active_pumps[chat_id] = pump
    q.release(claim)
    pump.start()

    async def _tell_parked() -> None:
        await tell_chat_waits(agent, chat_id, person, chat_row)

    # A _TaskTurnStalled reap persisted the partial turn and closed the chat
    # status — delivered as far as it went; never fall through to another rung
    # (that would run a SECOND echo turn).
    with contextlib.suppress(_TaskTurnStalled):
        await _watch_task_pump(layer, pump, echo_id, chat_id, session_id,
                                     on_parked=_tell_parked)
    if chat_turn:
        from core.events.pump_bg_monitors import arm_bg_monitors
        arm_bg_monitors(layer, session_id, chat_id)
    return ""
