"""The turns a restart cut: one closing row each when the platform comes back.

Every pump turn has an open-turn record from its start to its end
(``storage/chat/open_turns.py``). A process that ends without its shutdown,
or a graceful one that cut a turn before its engine's result, leaves the
record, so a record the boot finds is a turn nobody finished. Unless its
engine turn had finished (the pump only waited) or it was a meeting's (the
failed meeting's own row says what happened), it gets the chat's closing
row (``turn_ending.RESTARTED``: the reply was cut short, send a message to
continue) and the chat's abort flags, once, in the transaction that deletes
the record.

A turn on a machine waits: the satellite reports its sessions when it
reconnects, and a session it still holds is replayed into the chat
(``run_recovery``), which takes the record instead. One its machine does not
adopt, or that no report reaches before the recovery deadline, is closed
then.
"""

from __future__ import annotations

import functools
import json
import logging
import time
from datetime import datetime, timezone

from core import placement
from storage import database as task_store
from storage.chat import open_turns

logger = logging.getLogger("otodock.recovery")

# session id -> {"chat_id", "token", "machine_id", "deadline"}: the turns on
# a machine waiting for its report.
_parked: dict[str, dict] = {}

# This process's boot: a person's message after it means the chat moved on,
# and a closing row would land after that message.
_booted_at = datetime.now(timezone.utc).isoformat()


def _closing() -> tuple[str, str, bool | None]:
    """The closing row's event type and payload, and the abort flag."""
    from core.events import turn_ending
    from core.events.stream_pump import turn_ended_block
    from ws import wire_events as wire
    ending = turn_ending.TurnEnding(reason=turn_ending.RESTARTED)
    stamps = ending.abort_stamps()
    return (wire.SYSTEM, json.dumps(turn_ended_block(ending)),
            stamps["last_abort_graceful"] if stamps else None)


def close(chat_id: str, token: str) -> bool:
    """Write the closing row of the cut turn ``token`` and delete its record,
    unless a newer turn took the chat (``open_turns.close_cut``).
    Synchronous. True when the row was written."""
    event_type, event_data, graceful = _closing()
    written = open_turns.close_cut(chat_id, token, event_type, event_data,
                                   abort_graceful=graceful, since=_booted_at)
    if written:
        logger.info("chat=%s: the turn the restart cut got its closing row", chat_id[:8])
    return written


async def _on_lane(chat_id: str, job, label: str) -> None:
    from core.events import chat_writer
    try:
        await chat_writer.submit(chat_id, job, label=label)
    except Exception:  # one chat's close must not stop the others
        logger.exception("chat=%s: the closing row of the cut turn failed", chat_id[:8])


async def _close_on_lane(entry: dict) -> None:
    """``close`` on the chat's writer lane, in order with the chat's rows."""
    await _on_lane(entry["chat_id"], functools.partial(close, entry["chat_id"], entry["token"]),
                   "turn_cut_close")


async def close_taken(chat_id: str, turn_started: str) -> None:
    """The closing row of a cut turn whose record the replay took and whose
    replay never began (``turn_started``: the record's start)."""
    event_type, event_data, graceful = _closing()
    await _on_lane(chat_id, functools.partial(
        open_turns.write_closing, chat_id, event_type, event_data,
        abort_graceful=graceful, since=_booted_at, turn_started=turn_started), "turn_cut_close")


def defer_at_boot(deadline_s: float) -> tuple[int, int]:
    """Boot, before any satellite can report: every record is a turn the
    last process left. One whose engine turn had finished is deleted, no
    row, and so is a meeting's (a meeting pump records no session: the
    meeting's own failed row is its card). One on a machine with its session
    recorded waits ``deadline_s`` for the report (any reported turn may be
    replayed). Every other one is closed now. Returns ``(waiting, closed)``."""
    global _booted_at
    _booted_at = datetime.now(timezone.utc).isoformat()
    parked = closed = 0
    for rec in open_turns.list_open():
        chat_id, token, sid = rec["chat_id"], rec["turn_token"], rec["session_id"] or ""
        if not rec["engine_open"] or not sid:
            open_turns.end_turn(chat_id, token)
            continue
        target = (task_store.get_chat(chat_id) or {}).get("execution_target") or placement.LOCAL
        if sid and placement.machine_of(target):
            _parked[sid] = {"chat_id": chat_id, "token": token, "machine_id": target,
                            "deadline": time.monotonic() + deadline_s}
            parked += 1
        elif close(chat_id, token):
            closed += 1
    return parked, closed


def waiting(session_id: str) -> bool:
    """A cut turn of this session waits for its machine's report."""
    return session_id in _parked


def claim(session_id: str) -> dict | None:
    """An adoption replays this session's turn: its waiting entry, taken.
    Called before the adoption's first await, so no settle sees it."""
    return _parked.pop(session_id, None)


async def close_claimed(entry: dict | None) -> None:
    """A claimed entry whose replay did not start after all."""
    if entry is not None:
        await _close_on_lane(entry)


async def settle_machine(machine_id: str) -> None:
    """The machine reported and adopted what it adopts: every entry of it
    still waiting is closed."""
    for sid in [s for s, e in _parked.items() if e["machine_id"] == machine_id]:
        await _close_on_lane(_parked.pop(sid))


async def sweep_expired() -> None:
    """Entries whose machine never reported before the deadline, closed."""
    now = time.monotonic()
    for sid in [s for s, e in _parked.items() if now > e["deadline"]]:
        entry = _parked.pop(sid, None)
        if entry is not None:
            await _close_on_lane(entry)
