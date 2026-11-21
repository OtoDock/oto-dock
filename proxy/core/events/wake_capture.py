"""CLI self-wake turn capture (Claude Code ≥2.1.243, platform 1.5).

Since 2.1.243 a headless (``-p``) Claude session WAKES ITSELF when a
background Bash command or background subagent completes while the session
is idle: the CLI runs a full, unprompted model turn — bracketed by its own
``system/init`` … ``result`` frames — that reviews the completion ("the
2.1.243 background-wake"). Empirical captures live in
``tests/fixtures/cli_wake`` (probe rig 2026-08-27; see
the 1.5 self-wake design round).

Before 1.5 the between-turns readers (``drain_bg_commands``, the
stale-output drains) DISCARDED those frames: the engine had already billed
the wake turn, the user never saw it, the CLI-internal conversation history
diverged from the platform transcript, and the platform's own bg nudge then
ran a second, redundant review turn. This module records a wake bracket as
a real chat turn instead:

  * frames are fed through a fresh ``ClaudeCLIEventTranslator`` (registries
    resolve exactly as in a live turn) and pumped through a standard
    ``ChatStreamPump`` — persistence, WS fan-out, live_state, usage row and
    the cumulative-cost delta all behave like any turn;
  * a ``bg_wake`` system marker row precedes the wake blocks so a history
    reload explains the unprompted assistant message;
  * the capture is recorded per-session so the bg monitors' nudges stand
    down (``recently_captured``) instead of double-reviewing.

The caller owns frame acquisition (local stdout vs the remote event queue)
and the session lock; this module owns translation + recording. When the
session has no chat row (bare probes), frames are consumed and registries
resolved, but nothing is recorded.
"""

import asyncio
import contextlib
import logging
import time
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING

import config
from core.events.common_events import PRODUCER_DONE, SYSTEM, CommonEvent
from core.session import session_kind
from ws import wire_events as wire

if TYPE_CHECKING:
    from core.layers.cli.translator import ClaudeCLIEventTranslator

logger = logging.getLogger("otodock")

# Hard ceiling for one wake bracket — a wake turn can run tools, so this is
# a runaway guard, not a latency budget (mirrors the bg monitors' 600s). A
# caller that passes its session's liveness gets the turn ceiling instead
# (``config.CLAUDE_TIMEOUT``): a wake turn is a turn.
WAKE_BRACKET_CEILING_S = 600.0

# Frame-gap timeout inside a bracket: the CLI streams continuously while a
# turn runs; a long silent gap means the bracket will not close (process
# died, satellite drain window closed) — flush partial and stop. A caller
# that passes its session's liveness is asked first: a tool of the wake turn
# is silent for as long as it runs, so a gap ends a live session's bracket
# only at the ceiling.
WAKE_FRAME_GAP_S = 30.0

# session_id → monotonic time of the last completed wake capture. Consulted
# by the bg monitors' grace window (a captured wake IS the review turn).
_last_wake_capture: dict[str, float] = {}

# session_id → the translator of the bracket being captured: an interrupt the
# session is asked for meanwhile ends that turn (``note_interrupt``).
_capturing: dict[str, "ClaudeCLIEventTranslator"] = {}


def is_wake_init(data: dict) -> bool:
    """True for the ``system/init`` frame that opens a CLI turn bracket.

    Between turns, an init can ONLY be a self-wake turn starting (user turns
    write their prompt first and their init is read by the turn reader)."""
    return (isinstance(data, dict) and data.get("type") == "system"
            and data.get("subtype") == "init")


def opens_turn(data: dict) -> bool:
    """A line read between turns that opens a turn of the main agent: its
    ``system/init``, or its first message where the CLI sends no ``init``
    (the 2.1.312 recording ``probe-bash-moved.jsonl``). Nothing of the main
    agent's runs between turns but a turn the engine started by itself (or a
    steer it took late), so its first such line opens one. A sub-agent's
    lines carry ``parent_tool_use_id``."""
    if not isinstance(data, dict) or data.get("parent_tool_use_id"):
        return False
    kind = data.get("type")
    return is_wake_init(data) or kind in ("stream_event", "assistant")


def recently_captured(session_id: str, within_s: float = 60.0) -> bool:
    """Whether a wake turn finished capturing within the last ``within_s``,
    or one is being recorded now (the review is under way)."""
    if session_id in _capturing:
        return True
    ts = _last_wake_capture.get(session_id)
    return ts is not None and (time.monotonic() - ts) <= within_s


def captured_at(session_id: str) -> float | None:
    """The monotonic time the session's last wake turn finished capturing."""
    return _last_wake_capture.get(session_id)


def note_interrupt(session_id: str) -> None:
    """The session was asked to stop the turn it runs: when that turn is one
    being captured, its closing result is the interruption, not an error of
    the engine (the translator's own rule for a turn it was told to stop)."""
    translator = _capturing.get(session_id)
    if translator is not None:
        translator.interrupted = True


def note_captured(session_id: str) -> None:
    _last_wake_capture[session_id] = time.monotonic()


def forget_session(session_id: str) -> None:
    """Session teardown hygiene (called from clear_session_liveness)."""
    _last_wake_capture.pop(session_id, None)


async def capture_wake_turn(
    session_id: str,
    first_frame: dict,
    read_frame: Callable[[float], Awaitable[dict | None]],
    *,
    source: str = "idle",
    alive: Callable[[], bool] | None = None,
    turn_guard: Callable[[], contextlib.AbstractContextManager[None]] | None = None,
    steered: Callable[[], bool] | None = None,
) -> bool:
    """Record one wake bracket, ``first_frame`` (the init) included.

    ``read_frame(timeout)`` returns the next PARSED stream-json frame or
    None on timeout/EOF — the caller keeps holding the session lock and
    reading from its own transport. ``alive`` says whether the session can
    still write a frame (its process runs, its stream is open): with it a
    silent gap ends the bracket only once it answers False, and the bracket
    has the turn ceiling. It must answer False at the stream's end, where
    ``read_frame`` returns at once. ``turn_guard`` marks the session's turn
    as running on its layer while this bracket is the chat's live turn, so
    a Stop and a queued message's interrupt meet it as they meet any turn.
    ``steered`` says a message was steered into this turn since its last
    result: the CLI may take it after that result as a turn of its own, which
    opens at once, so a turn that opens within the steer grace is read
    through as part of this one (``_peek_after_result``'s twin).
    Returns True when a bracket was recorded to a chat (False: no chat
    row, or the bracket never closed — partial content is still flushed in
    that case).
    """
    from core.events.stream_pump import ChatStreamPump, _active_pumps

    # Lazy: core.layers.cli.layer imports the session module, which calls us.
    from core.layers.cli.helpers import _STEER_INIT_GRACE_S
    from core.layers.cli.layer import cli_chunk_to_events
    from core.layers.cli.translator import ClaudeCLIEventTranslator
    from core.session.session_state import get_session_client_type
    from storage import database as task_store

    chat = None
    try:
        chat = task_store.get_chat_by_session(session_id)
    except Exception:
        logger.exception("wake capture: chat lookup failed for %s",
                         session_id[:8])

    translator = ClaudeCLIEventTranslator(session_id)
    _capturing[session_id] = translator
    pump: ChatStreamPump | None = None
    pump_task: asyncio.Task | None = None
    registered = False
    queue: asyncio.Queue | None = None

    from core.session import engine_prompts

    def _feed(data: dict) -> None:
        engine_prompts.note_frame(session_id, data, in_turn=True)
        for chunk in translator.feed(data):
            for ev in cli_chunk_to_events(chunk):
                if queue is not None:
                    queue.put_nowait(ev)

    ceiling = WAKE_BRACKET_CEILING_S if alive is None else float(config.CLAUDE_TIMEOUT)

    async def _read_bracket() -> bool:
        """The bracket's frames, read to its result: the pump's producer, so
        a viewer's attach finds a running turn and a Stop cancels this."""
        deadline = time.monotonic() + ceiling
        _feed(first_frame)
        while time.monotonic() < deadline:
            data = await read_frame(WAKE_FRAME_GAP_S)
            if data is None:
                if alive is not None and alive():
                    continue  # a silent tool of a live session
                break  # gap/EOF — bracket will not close here
            _feed(data)
            if data.get("type") == "result":
                if steered is None or not steered():
                    return True
                # The steered message's own turn, if the CLI took it after
                # this result: read through; anything else ends the turn.
                nxt = await read_frame(_STEER_INIT_GRACE_S)
                if nxt is None:
                    return True
                _feed(nxt)
                if not opens_turn(nxt):
                    return True
        return False

    if chat is not None:
        chat_id = chat["id"]
        queue = asyncio.Queue()
        producer = asyncio.create_task(_read_bracket())
        pump = ChatStreamPump(
            chat_id=chat_id,
            session_id=session_id,
            producer=producer,
            event_queue=queue,
            perm_queue=None,
            scope=chat.get("scope") or "user",
            # The DRIVER's kind, never the row's: a wake into the scheduler's
            # own session bills as a task turn; a dashboard session's as a
            # chat turn (and flips a finished run around it); a phone chat's
            # pump stays one the dashboard never attaches to.
            source_type=session_kind.driver_source_type(
                get_session_client_type(session_id), chat),
        )
        pump.records_engine_turn = True
        # Register for live streaming ONLY when the chat has no active pump.
        # A stale-drain capture runs with the NEXT user turn's pump already
        # registered — never displace it; the wake blocks still persist and
        # appear on reload/history (rare, seconds-wide race).
        if chat_id not in _active_pumps:
            _active_pumps[chat_id] = pump
            registered = True
            # A message sent during this turn interrupts it, as during any
            # turn: the pump's end hands the chat's queue on at once.
            pump.stop_and_send = {"fired": False, "note": False}
        pump_task = pump.start()
        # Provenance marker row — history reload must explain the
        # unprompted assistant turn that follows.
        queue.put_nowait(CommonEvent(type=SYSTEM, data={
            "subtype": wire.SUBTYPE_BG_WAKE,
            "message": "Background work finished — reviewing the result.",
            "source": source,
        }))

    closed = False
    guard = turn_guard() if (registered and turn_guard is not None) else contextlib.nullcontext()
    try:
        with guard:
            if pump is None:
                closed = await _read_bracket()
            else:
                await asyncio.wait({producer})
                if not producer.cancelled():
                    if producer.exception() is not None:
                        logger.error("wake capture: reading the bracket of %s failed",
                                     session_id[:8], exc_info=producer.exception())
                    else:
                        closed = producer.result()
        if not closed:
            logger.warning(
                "wake capture: bracket for %s did not close (source=%s) — "
                "flushing partial", session_id[:8], source,
            )
    finally:
        if _capturing.get(session_id) is translator:
            del _capturing[session_id]
        if pump is not None and queue is not None:
            queue.put_nowait(CommonEvent(type=PRODUCER_DONE, data={}))
            try:
                if pump_task is not None:
                    await asyncio.wait_for(pump_task, timeout=30.0)
            except Exception:
                logger.exception("wake capture: pump finalize failed for %s",
                                 session_id[:8])
            if registered and _active_pumps.get(pump.chat_id) is pump:
                _active_pumps.pop(pump.chat_id, None)
        if closed:
            note_captured(session_id)
            logger.info(
                "wake capture: recorded self-wake turn for %s (source=%s, "
                "recorded=%s)", session_id[:8], source, chat is not None,
            )
    return closed and chat is not None
