"""Turn-counter memory-capture nudge.

After ``nudge_turns`` user turns in a chat session without a single
``memory`` tool call, a one-line reminder rides the NEXT user message
(appended at the dashboard send point, AFTER the user message was
persisted — the reminder never renders in the chat UI or DB). Any memory
tool call resets the counter (the stream pump reports it from the uniform
CommonEvent stream, so all execution layers count identically).

Costs ~30 input tokens on an existing turn — no extra turns, no schedule.
``nudge_turns`` lives in ``memory_settings`` (default 10, 0 = off).

Chat sessions only by design: tasks are usually single-turn and carry the
directive in their prompt; phone sessions are short-lived and TTS-bound.
"""

from __future__ import annotations

from collections import OrderedDict

NUDGE_TEXT = (
    "Reminder: if this session has surfaced durable facts, preferences, or "
    "decisions, save them with the `memory` tool now — update existing "
    "topics rather than adding duplicates. If nothing is worth saving, "
    "carry on; no need to mention this."
)

# session_id → user turns since the last memory tool call. Opportunistically
# pruned (oldest first) so abandoned sessions can't grow it unbounded.
_counters: "OrderedDict[str, int]" = OrderedDict()
_MAX_TRACKED_SESSIONS = 4096


def _threshold() -> int:
    try:
        from storage.agents import memory_store
        return int(memory_store.get_settings().get("nudge_turns") or 0)
    except Exception:
        return 0


def maybe_nudge(session_id: str) -> str | None:
    """Count this user turn; return the reminder line when the threshold is
    crossed (and reset, so it re-arms after another N turns). None otherwise
    or when the feature is off."""
    threshold = _threshold()
    if threshold <= 0:
        return None
    count = _counters.get(session_id, 0) + 1
    _counters[session_id] = count
    _counters.move_to_end(session_id)
    while len(_counters) > _MAX_TRACKED_SESSIONS:
        _counters.popitem(last=False)
    if count >= threshold:
        _counters[session_id] = 0
        return NUDGE_TEXT
    return None


def record_memory_call(session_id: str) -> None:
    """A memory tool call landed — the agent is maintaining memory; reset."""
    if session_id in _counters:
        _counters[session_id] = 0


def forget(session_id: str) -> None:
    """Drop a closed session's counter."""
    _counters.pop(session_id, None)
