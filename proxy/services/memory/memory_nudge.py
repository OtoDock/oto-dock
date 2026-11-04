"""Memory-capture nudge: a turn counter, and an arm after a compaction.

After ``nudge_turns`` user turns in a chat session without a memory WRITE,
a one-line reminder rides the NEXT user message (appended at the dashboard
send point, AFTER the user message was persisted — the reminder never
renders in the chat UI or DB). A write that landed resets the counter
(the memory API reports it, so every execution layer counts identically;
a ``view`` is not maintenance). A completed context compaction arms the
session: the next message carries the reminder once, whatever the count,
because that is the moment the engine forgot what it had not saved.

Costs ~30 input tokens on an existing turn — no extra turns, no schedule.
``nudge_turns`` lives in ``memory_settings`` (default 10, 0 = off).

Chat sessions only by design: tasks are usually single-turn and carry the
directive in their prompt; phone sessions are short-lived and TTS-bound.
"""

from __future__ import annotations

from collections import OrderedDict

import psycopg

NUDGE_TEXT = (
    "Reminder: if this session has surfaced durable facts, preferences, or "
    "decisions, save them with the `memory` tool now — update existing "
    "topics rather than adding duplicates. If nothing is worth saving, "
    "carry on; no need to mention this."
)

# session_id → user turns since the last memory write, and the sessions a
# compaction armed. Both opportunistically pruned (oldest first) so
# abandoned sessions can't grow them unbounded.
_counters: OrderedDict[str, int] = OrderedDict()
_armed: OrderedDict[str, None] = OrderedDict()
_MAX_TRACKED_SESSIONS = 4096


def _threshold() -> int:
    try:
        from storage.agents import memory_store
        return int(memory_store.get_settings().get("nudge_turns") or 0)
    except (psycopg.Error, ValueError):
        return 0


def _prune(table: OrderedDict) -> None:
    while len(table) > _MAX_TRACKED_SESSIONS:
        table.popitem(last=False)


def maybe_nudge(session_id: str) -> str | None:
    """Count this user turn; return the reminder line when the threshold is
    crossed or a compaction armed the session (and reset, so it re-arms
    after another N turns). None otherwise or when the feature is off."""
    threshold = _threshold()
    if threshold <= 0:
        _armed.pop(session_id, None)
        return None
    armed = session_id in _armed
    _armed.pop(session_id, None)
    count = _counters.get(session_id, 0) + 1
    _counters[session_id] = count
    _counters.move_to_end(session_id)
    _prune(_counters)
    if armed or count >= threshold:
        _counters[session_id] = 0
        return NUDGE_TEXT
    return None


def arm(session_id: str) -> None:
    """A compaction completed: the next message reminds once."""
    _armed[session_id] = None
    _armed.move_to_end(session_id)
    _prune(_armed)


def record_memory_call(session_id: str) -> None:
    """A memory write landed — the agent is maintaining memory; reset."""
    if session_id in _counters:
        _counters[session_id] = 0


def forget(session_id: str) -> None:
    """Drop a closed session's counter and arm."""
    _counters.pop(session_id, None)
    _armed.pop(session_id, None)
