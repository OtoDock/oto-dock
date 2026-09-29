"""The task run's status vocabulary — ``task_runs.status`` named once
(core-seams phase 8) — and the delegate result beside it.

Six stored values, every spelling frozen (the column, ``GET
/v1/tasks/runs*``, the run stream's ``status`` / ``done`` frames, the chat
list's ``run_status``, the catalog ``tasks`` feed, the schedules MCP's
texts): ``pending`` (the row ``create_run`` mints), ``running`` (the slot
admitted it), and the four ends ``completed`` / ``failed`` / ``cancelled``
/ ``limit_exceeded``. Every writer goes through ``db_tasks.update_run`` or
the two raw-SQL helpers beside it; generic code asks :func:`is_live` /
:func:`is_terminal` or compares the constants. ``timeout`` was never
written by anything in the tree and is not a member: the schedules MCP's
``timeout`` is its own wait word, the judge kind's is the wait's outcome.

The delegate result is what a delegate lane reports to its caller — the
``delegate_result`` frame and event row, the callback prompt's
``{{status}}``, the live delegate block (``running`` before it): the three
ends plus ``user_interrupted``, minted from the worker chat's abort flag
and never stored in ``task_runs.status`` (the row of an interrupted lane
reads ``completed``, ``cancelled`` or ``failed``).

Stdlib only — a leaf every side imports at module level, module-qualified
(``from storage.automation import run_status``).
"""

from __future__ import annotations

PENDING = "pending"
RUNNING = "running"
COMPLETED = "completed"
FAILED = "failed"
CANCELLED = "cancelled"
LIMIT_EXCEEDED = "limit_exceeded"

STATUSES: frozenset[str] = frozenset({PENDING, RUNNING, COMPLETED, FAILED, CANCELLED, LIMIT_EXCEEDED})

#: A run that has not ended: queued or in its slot. The restart recovery,
#: the delete guard, the spawn cap and the rename broadcast ask this.
LIVE: frozenset[str] = frozenset({PENDING, RUNNING})

#: The ends. ``update_run`` tells the finished-run listeners after a
#: terminal write; a dashboard-resumed task chat reopens a row FROM one of
#: these; the run stream replays a row already here.
TERMINAL: frozenset[str] = frozenset({COMPLETED, FAILED, CANCELLED, LIMIT_EXCEEDED})


def is_live(status: str | None) -> bool:
    """Queued or running."""
    return status in LIVE


def is_terminal(status: str | None) -> bool:
    """Ended — whatever the end."""
    return status in TERMINAL


# ---------------------------------------------------------------------------
# The delegate result
# ---------------------------------------------------------------------------

#: The user stopped or redirected the worker lane (``chats.last_turn_aborted``).
USER_INTERRUPTED = "user_interrupted"

#: What a delegate lane reports to its caller. Never a stored run status.
DELEGATE_RESULTS: frozenset[str] = frozenset({COMPLETED, FAILED, CANCELLED, USER_INTERRUPTED})
