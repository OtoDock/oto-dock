"""The meeting's status vocabulary — ``meetings.status`` named once
(core-seams phase 8).

Six stored values, every spelling frozen (the column, DEFAULT ``pending``;
``/v1/meetings*``): ``pending`` (created, not started), ``active`` (the
round loop runs), ``paused`` (a participant proposed to conclude; only the
moderator speaks next), ``concluding`` (an end was requested; the loop
finishes the round), ``concluded`` and ``failed`` (the ends). Every write
goes through ``db_meetings.update_meeting`` (which refuses a word outside
the set) or the orphan sweep beside it; generic code asks the sets below
or :func:`is_terminal`.

The route preconditions and the machine's own are two questions (the
route reads the row, awaits, and the orchestrator re-reads it): a caller
may REQUEST an end on ``active`` or ``concluding`` and a leave on
``active``; the machine ENDS from ``paused`` too (the moderator's
``end_meeting`` landing after a ``propose_conclude``) and LEAVES from
``paused`` too. ``LIVE`` is what counts toward the creator's cap, what
"one meeting per chat" reads and what the restart sweep fails — ``paused``
is outside it (a paused meeting survives a restart neither failed nor
live; documented, unchanged).

Stdlib only — a leaf, used module-qualified (``from storage.chat import
meeting_status``).
"""

from __future__ import annotations

PENDING = "pending"
ACTIVE = "active"
PAUSED = "paused"
CONCLUDING = "concluding"
CONCLUDED = "concluded"
FAILED = "failed"

STATUSES: frozenset[str] = frozenset({PENDING, ACTIVE, PAUSED, CONCLUDING, CONCLUDED, FAILED})

#: Counts toward the creator's participant cap, is "the meeting on this
#: chat", is failed by the restart sweep.
LIVE: frozenset[str] = frozenset({PENDING, ACTIVE, CONCLUDING})

#: The ends.
TERMINAL: frozenset[str] = frozenset({CONCLUDED, FAILED})

#: The round loop stops queueing turns.
ROUND_STOPS: frozenset[str] = frozenset({CONCLUDING, FAILED})

#: The machine's own: an end / a leave from these states.
ENDABLE: frozenset[str] = frozenset({ACTIVE, CONCLUDING, PAUSED})
LEAVABLE: frozenset[str] = frozenset({ACTIVE, PAUSED})

#: The routes' preconditions: a caller may request these from these states.
REQUEST_ENDABLE: frozenset[str] = frozenset({ACTIVE, CONCLUDING})
REQUEST_LEAVABLE: frozenset[str] = frozenset({ACTIVE})
PROPOSABLE: frozenset[str] = frozenset({ACTIVE})

#: Where each writer may take a row (the ledger's table; the writers keep
#: their own guards — this is data, ``update_meeting`` checks membership).
TRANSITIONS: dict[str, frozenset[str]] = {
    PENDING: frozenset({ACTIVE, FAILED}),
    ACTIVE: frozenset({PAUSED, CONCLUDING, CONCLUDED, FAILED}),
    PAUSED: frozenset({ACTIVE, CONCLUDING, CONCLUDED}),
    CONCLUDING: frozenset({CONCLUDED, FAILED}),
    CONCLUDED: frozenset(),
    FAILED: frozenset(),
}


def is_terminal(status: str | None) -> bool:
    """Concluded or failed."""
    return status in TERMINAL
