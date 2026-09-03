"""CLI self-wake turn capture (Claude Code ≥2.1.243, platform 1.5).

Since 2.1.243 a headless (``-p``) Claude session WAKES ITSELF when a
background Bash command or background subagent completes while the session
is idle: the CLI runs a full, unprompted model turn — bracketed by its own
``system/init`` … ``result`` frames — that reviews the completion ("the
2.1.243 background-wake"). Empirical captures live in
``tests/session/fixtures`` (probe rig 2026-08-27; see
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
import logging
import time
from typing import Awaitable, Callable

from core.events.common_events import PRODUCER_DONE, SYSTEM, CommonEvent

logger = logging.getLogger("claude-proxy")

# Hard ceiling for one wake bracket — a wake turn can run tools, so this is
# a runaway guard, not a latency budget (mirrors the bg monitors' 600s).
WAKE_BRACKET_CEILING_S = 600.0

# Frame-gap timeout inside a bracket: the CLI streams continuously while a
# turn runs; a long silent gap means the bracket will not close (process
# died, satellite drain window closed) — flush partial and stop.
WAKE_FRAME_GAP_S = 30.0

# session_id → monotonic time of the last completed wake capture. Consulted
# by the bg monitors' grace window (a captured wake IS the review turn).
_last_wake_capture: dict[str, float] = {}


def is_wake_init(data: dict) -> bool:
    """True for the ``system/init`` frame that opens a CLI turn bracket.

    Between turns, an init can ONLY be a self-wake turn starting (user turns
    write their prompt first and their init is read by the turn reader)."""
    return (isinstance(data, dict) and data.get("type") == "system"
            and data.get("subtype") == "init")


def recently_captured(session_id: str, within_s: float = 60.0) -> bool:
    """Whether a wake turn finished capturing within the last ``within_s``."""
    ts = _last_wake_capture.get(session_id)
    return ts is not None and (time.monotonic() - ts) <= within_s


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
) -> bool:
    """Record one wake bracket, ``first_frame`` (the init) included.

    ``read_frame(timeout)`` returns the next PARSED stream-json frame or
    None on timeout/EOF — the caller keeps holding the session lock and
    reading from its own transport. Returns True when a bracket was
    recorded to a chat (False: no chat row, or the bracket never closed —
    partial content is still flushed in that case).
    """
    from storage import database as task_store
    # Lazy: core.layers.cli.layer imports the session module, which calls us.
    from core.layers.cli.layer import cli_chunk_to_events
    from core.layers.cli.translator import ClaudeCLIEventTranslator
    from core.events.stream_pump import ChatStreamPump, _active_pumps

    chat = None
    try:
        chat = task_store.get_chat_by_session(session_id)
    except Exception:
        logger.exception("wake capture: chat lookup failed for %s",
                         session_id[:8])

    translator = ClaudeCLIEventTranslator(session_id)
    pump: ChatStreamPump | None = None
    pump_task: asyncio.Task | None = None
    registered = False
    queue: asyncio.Queue | None = None

    if chat is not None:
        chat_id = chat["id"]
        queue = asyncio.Queue()
        # The producer handle only anchors pump cancellation semantics — the
        # frames are pushed by THIS coroutine, so a no-op task suffices.
        producer = asyncio.create_task(asyncio.sleep(0))
        pump = ChatStreamPump(
            chat_id=chat_id,
            session_id=session_id,
            producer=producer,
            event_queue=queue,
            perm_queue=None,
            scope=chat.get("scope") or "user",
            source_type=chat.get("source_type") or "chat",
        )
        # Register for live streaming ONLY when the chat has no active pump.
        # A stale-drain capture runs with the NEXT user turn's pump already
        # registered — never displace it; the wake blocks still persist and
        # appear on reload/history (rare, seconds-wide race).
        if chat_id not in _active_pumps:
            _active_pumps[chat_id] = pump
            registered = True
        pump_task = pump.start()
        # Provenance marker row — history reload must explain the
        # unprompted assistant turn that follows.
        queue.put_nowait(CommonEvent(type=SYSTEM, data={
            "subtype": "bg_wake",
            "message": "Background work finished — reviewing the result.",
            "source": source,
        }))

    def _feed(data: dict) -> None:
        for chunk in translator.feed(data):
            for ev in cli_chunk_to_events(chunk):
                if queue is not None:
                    queue.put_nowait(ev)

    closed = False
    deadline = time.monotonic() + WAKE_BRACKET_CEILING_S
    _feed(first_frame)
    try:
        while time.monotonic() < deadline:
            data = await read_frame(WAKE_FRAME_GAP_S)
            if data is None:
                break  # gap/EOF — bracket will not close here
            _feed(data)
            if data.get("type") == "result":
                closed = True
                break
        if not closed:
            logger.warning(
                "wake capture: bracket for %s did not close (source=%s) — "
                "flushing partial", session_id[:8], source,
            )
    finally:
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
