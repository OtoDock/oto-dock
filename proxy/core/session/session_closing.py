"""A session's close and the next start of its id never overlap.

A close removes its session's record first and clears what is kept by the
session's id last: the machine's queue, the permission state, the prompt
file, the caches, the seat and the slot. Between the two it waits (for the
engine's state, for the machine's ack, for the process). A start of the same
id that ran inside that wait owned all of those by the time the close's tail
cleared them: the new process lost its prompt file and its state.

So a close holds its id while it runs (``closing``) and every start of an id
waits for the hold first (``wait_closed``): the layers' ``start_session`` and
the remote layer's two adoptions. A hold older than ``HOLD_MAX_S`` no longer
holds, so a close that hangs never keeps a chat from starting.

The other direction, a close that finds no session of its own while a start
of the id is in flight, is each layer's own line: it reads the start's mark
(``session_state.is_starting``) and clears nothing.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from collections.abc import Iterator

logger = logging.getLogger("otodock.session")

#: How long one close holds its id at most.
HOLD_MAX_S = 60.0
_POLL_S = 0.05

# session id -> the start times (monotonic) of its closes in flight.
_holds: dict[str, list[float]] = {}


@contextlib.contextmanager
def closing(session_id: str) -> Iterator[None]:
    """Hold ``session_id`` for one close, from the removal of its record to
    its last line. Counted: a second close of the id holds beside the first."""
    if not session_id:
        yield
        return
    began = time.monotonic()
    _holds.setdefault(session_id, []).append(began)
    try:
        yield
    finally:
        held = _holds.get(session_id)
        if held is not None:
            with contextlib.suppress(ValueError):
                held.remove(began)
            if not held:
                _holds.pop(session_id, None)


def is_closing(session_id: str) -> bool:
    """A close of ``session_id`` is in flight and still holds it."""
    held = _holds.get(session_id)
    if not held:
        return False
    now = time.monotonic()
    return any(now - began < HOLD_MAX_S for began in held)


async def wait_closed(session_id: str) -> None:
    """Wait for every close of ``session_id`` in flight, then for the disk work
    its closes left behind (``file_commit.behind``: its folders, its
    descriptor). Returns at once when there is none, and within
    ``HOLD_MAX_S`` for both together."""
    began = time.monotonic()
    if is_closing(session_id):
        while is_closing(session_id):  # noqa: ASYNC110  # the hold ends in a close's finally, on any loop: a poll misses none
            await asyncio.sleep(_POLL_S)
        waited = time.monotonic() - began
        if _holds.get(session_id):
            logger.warning("session %s: its close still runs after %.0f s, the start goes on",
                           session_id[:8], waited)
        else:
            logger.info("session %s: its start waited %.2f s for its close", session_id[:8], waited)
    from core import file_commit
    await file_commit.settled(session_id, HOLD_MAX_S - (time.monotonic() - began))


def reset() -> None:
    """Forget every hold (tests)."""
    _holds.clear()
