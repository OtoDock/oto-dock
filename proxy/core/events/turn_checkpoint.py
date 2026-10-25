"""What a turn checkpoint saves and what it leaves live.

A pump saves its turn's blocks at the turn's boundaries (a steer, a drained
queue turn, the end). A long turn also saves while it runs, every
``CHECKPOINT_S`` (``due_upto``, asked by ``ChatStreamPump._maybe_checkpoint``): only the leading
blocks the turn can no longer change or move (``settled_prefix``), so the rows
of a turn that ends normally are the rows it would have written without
checkpoints, in the same order. The checklist block changes in place for the
whole segment and is saved anyway: its row is rewritten when it changes
(``RowRef``, ``rewrite_row``).

A save trims the live blocks its rows now carry from the chat's live state
(``trim_list``), by identity: a block is either in history up to the cutoff or
in the live state, never both, never neither. A turn block and the live block
that shows it are the same dict, except for four pairs the handlers build
from two dicts, recorded in the pump's twin map: a text segment and the live
text block that holds its words, a subagent spawn and its live agent, a
background command and its live command, a delegation and its live delegate.

The pump owns the state; a rewrite's job goes on the chat's writer lane,
behind the checkpoint's own.
"""

from __future__ import annotations

import asyncio
import functools
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from typing import TYPE_CHECKING

from core.events import artifact_events, chat_writer
from core.events.common_events import (
    BG_COMMAND_START,
    CONTEXT_COMPACT,
    DELEGATE_SPAWN,
    PERMISSION_REQUEST,
    PLAN_MODE,
    SUBAGENT_START,
    TEXT,
    THINKING,
    TOOL_USE,
    CommonEvent,
)
from core.session.session_state import prompt_waits
from storage import database as task_store
from storage.chat import open_turns
from ws import wire_events as wire

if TYPE_CHECKING:
    from core.events.stream_pump import ChatStreamPump

#: The least time between two saves of one turn before a checkpoint runs.
#: Time, not a block count: a count would save a busy tool turn every few
#: seconds and never a slow one with few big blocks; a time bounds what a
#: cut loses.
CHECKPOINT_S = 30.0

#: How long a plan review stays unsettled after its card left the screen:
#: the answer writes its action a moment later, after an await.
REVIEW_ANSWER_S = 5.0

#: The events only an engine turn in progress produces: one after the
#: engine's result is its next turn (a system prompt reaches the engine with
#: no pump event of its own). A compaction too, but only its start and its
#: end: its gauge reading may follow a turn that finished (``opens_engine_turn``).
ENGINE_TURN_EVENTS = frozenset({TEXT, THINKING, TOOL_USE, SUBAGENT_START, BG_COMMAND_START,
                                DELEGATE_SPAWN, PERMISSION_REQUEST, PLAN_MODE, CONTEXT_COMPACT})
_COMPACTION_IN_TURN = frozenset({"started", "completed"})

#: The field a foreground subagent's report fills on its spawn block.
_REPORT = "tool_result"

#: ``id(turn block) -> (turn block, live block)`` for the pairs built from
#: two dicts (the turn block is kept so a recycled id never matches).
Twins = dict[int, tuple[dict, dict]]


@dataclass(eq=False)
class RowRef:
    """The row a checkpoint wrote for the checklist block."""
    #: The row's id, set by the checkpoint's job on the chat's lane (a
    #: rewrite job queued after it reads it there); 0 when the row was refused.
    id: int = 0


@dataclass(frozen=True)
class Stops:
    """What the pump knows about the blocks that may still change: a plan
    review's prompt pending (or just answered), the foreground subagents
    whose spawn is done (its report came, or its tool or its run ended), the
    held previews (identity -> block)."""
    review_pending: Callable[[str], bool]
    spawns_done: frozenset[str] | set[str]
    held: dict[str, dict]


def rowed(block: dict) -> bool:
    """A block that gets a row (a transient artifact kind never does:
    ``stream_pump._serialize_turn_rows``)."""
    artifact = artifact_events.kind_of(block["type"])
    return artifact is None or artifact.saved


def unsettled(block: dict, stops: Stops) -> bool:
    """A block the turn may still change or remove in a way no row rewrite
    follows: a plan review whose answer is pending, a saved placeholder its
    artifact evicts, a foreground subagent whose report may still attach, a
    held preview a later push replaces."""
    kind = block.get("type")
    if kind == wire.PLAN_REVIEW:
        return "action" not in block and stops.review_pending(str(block.get("request_id") or ""))
    if kind == wire.TASK_SPAWN:
        return (not block.get("run_in_background") and _REPORT not in block
                and block.get("tool_use_id") not in stops.spawns_done)
    artifact = artifact_events.kind_of(kind)
    if artifact is None or not artifact.saved:
        return False
    if artifact.placeholder:
        return True
    return bool(artifact.deferred and artifact.identity
                and stops.held.get(block.get(artifact.identity) or "") is block)


def live_of(block: dict, twins: Twins) -> dict:
    """The live block that shows ``block``: its twin, else itself (a turn-only
    block is in no live list, so asking for it matches nothing)."""
    entry = twins.get(id(block))
    return entry[1] if entry is not None and entry[0] is block else block


def settled_prefix(blocks: list[dict], open_tools: Iterable[dict], twins: Twins,
                   stops: Stops) -> int:
    """How many leading blocks a checkpoint may take: up to the start
    position of the first open tool (it lands there when it closes) and the
    first unsettled block, then back before any text segment whose live text
    block also holds words of a segment after the cut (one live block is
    trimmed whole or not at all). The caller saves the ones with a row and
    keeps the others in front."""
    k = len(blocks)
    for tool in open_tools:
        k = min(k, int(tool.get("_insert_idx", k)))
    for i, block in enumerate(blocks[:k]):
        if unsettled(block, stops):
            k = i
            break
    later = {id(live_of(b, twins)) for b in blocks[k:] if b.get("type") == wire.TEXT}
    for i, block in enumerate(blocks[:k]):
        if block.get("type") == wire.TEXT and id(live_of(block, twins)) in later:
            return i
    return k


def opens_engine_turn(event: CommonEvent) -> bool:
    """An event an engine turn in progress produces (``ENGINE_TURN_EVENTS``)."""
    if event.type == CONTEXT_COMPACT:
        return event.data.get("phase") in _COMPACTION_IN_TURN
    return event.type in ENGINE_TURN_EVENTS


def review_pending(pump: ChatStreamPump, request_id: str) -> bool:
    """A plan review's prompt is shown, waiting, buffered, or was answered a
    moment ago (its action is written after an await)."""
    active = pump._permission_active or {}
    answered = pump._reviews_answered.get(request_id)
    return (active.get("request_id") == request_id or prompt_waits(request_id)
            or any(p.get("request_id") == request_id for p in pump._permission_buffer)
            or (answered is not None and time.monotonic() - answered < REVIEW_ANSWER_S))


def due_upto(pump: ChatStreamPump, *, suppressed: bool) -> int:
    """How many leading blocks a checkpoint takes now, 0 for none: only
    ``CHECKPOINT_S`` after the turn's last save, at an event boundary (no
    words pending), one checkpoint on the lane at a time, never for a turn
    the replay writes (``suppressed``)."""
    if not pump._turn_blocks or suppressed or pump._done or pump._pending_text:
        return 0
    last, running = pump._last_save_at, pump._checkpoint_fut
    if (last is None or asyncio.get_running_loop().time() - last < CHECKPOINT_S
            or (running is not None and not running.done())):
        return 0
    stops = Stops(functools.partial(review_pending, pump), pump._spawns_done, pump._pending_previews)
    return settled_prefix(pump._turn_blocks, pump._active_tools.values(), pump._live_twins, stops)


def trim_list(live_list: list[dict], kept: Iterable[dict], open_tools: Iterable[dict],
              twins: Twins) -> list[dict]:
    """The live blocks a save's rows now carry: every live block but the open
    tools and the ones showing a turn block still unsaved (``kept``). Taken at
    the submission; the list keeps the dicts alive, so their ids stay theirs
    until the landing removes them."""
    keep = {id(t) for t in open_tools} | {id(live_of(b, twins)) for b in kept}
    return [b for b in live_list if id(b) not in keep]


def todo_ref(saved: list[dict], todo_block: dict | None) -> tuple[int, RowRef] | None:
    """The checklist block's row among a checkpoint's rows: its index, a
    reference to fill."""
    if todo_block is None:
        return None
    for i, block in enumerate(b for b in saved if rowed(b)):
        if block is todo_block:
            return i, RowRef()
    return None


def rewrite_row(chat_id: str, ref: RowRef, event_data: str) -> None:
    """Rewrite ``ref``'s row with ``event_data`` on the chat's lane, after
    the checkpoint's job that set its id."""
    def _job() -> None:
        if ref.id:
            task_store.update_chat_message_event_data(chat_id, ref.id, event_data)

    chat_writer.submit(chat_id, _job, label="turn_row_rewrite")


def note_engine_turn(chat_id: str, token: str, *, running: bool) -> None:
    """The open-turn record follows the engine's turns (``token``: the
    pump's, "" when it writes no record): a running one's early ranges start
    over, a finished one's rows are final."""
    if token:
        job = open_turns.engine_turn_began if running else open_turns.engine_turn_done
        chat_writer.submit(chat_id, functools.partial(job, chat_id, token), label="engine_turn")
