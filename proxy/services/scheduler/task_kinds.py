"""The task kinds — one leaf for the three vocabularies a task and its runs
carry (core-seams phase 9), used module-qualified (``from services.scheduler
import task_kinds``; ``task_kinds.TRIGGER``).

Three columns, three vocabularies, frozen spellings:

* ``dynamic_tasks.task_type`` — the DEFINITION's kind: ``scheduled`` (a cron
  or an interval), ``one_time`` (a ``run_at`` or a delay), ``trigger`` (fired
  only by a webhook trigger; no clock), ``continuation`` (a bounded wake of a
  chat), ``app`` (an app handler's schedule) and ``delegate`` (a spawned
  worker); ``check`` is a judge run's in-memory kind and never a row.
* ``task_runs.task_type`` — the RUN's kind, the word ``run_kind_of`` stamps:
  ``scheduled``, ``one-time`` (the hyphen is the run column's own spelling,
  older than the definition column's underscore; both are frozen),
  ``trigger``, ``delegate``, ``app``, ``check``.
* ``task_runs.trigger_type`` — the ORIGIN of one run: ``scheduled`` (the
  clock), ``manual`` (Run now, a delegation), ``trigger`` (a webhook trigger
  fired), ``check`` (a judge), ``app_handler`` / ``app_action`` (an app).

Generic code asks the facts (``of(task).fires``, ``self_removes(task)``,
``spaced(trigger_type)``, ``run_allows_knowledge_rw(word)``) or compares the
constants; a literal is legal only here (``tests/core/test_kind_surface.py``).
The dashboard mirror is ``dashboard/src/lib/kinds/task.ts``
(``tests/core/test_kinds.py`` binds it). Stdlib only: the standalone
scheduler imports this package without the platform.
"""

from __future__ import annotations

from dataclasses import dataclass

# ---------------------------------------------------------------------------
# the definition's kind (dynamic_tasks.task_type)
# ---------------------------------------------------------------------------

SCHEDULED = "scheduled"
ONE_TIME = "one_time"
TRIGGER = "trigger"
CONTINUATION = "continuation"
APP = "app"
DELEGATE = "delegate"
CHECK = "check"

# ---------------------------------------------------------------------------
# the run's kind (task_runs.task_type)
# ---------------------------------------------------------------------------

RUN_SCHEDULED = "scheduled"
RUN_ONE_TIME = "one-time"
RUN_TRIGGER = "trigger"
RUN_DELEGATE = "delegate"
RUN_APP = "app"
RUN_CHECK = "check"

# ---------------------------------------------------------------------------
# the origin (task_runs.trigger_type)
# ---------------------------------------------------------------------------

TRIGGER_SCHEDULED = "scheduled"
TRIGGER_MANUAL = "manual"
TRIGGER_TRIGGER = "trigger"
TRIGGER_CHECK = "check"
TRIGGER_APP_HANDLER = "app_handler"
TRIGGER_APP_ACTION = "app_action"
TRIGGER_KINDS = frozenset({TRIGGER_SCHEDULED, TRIGGER_MANUAL, TRIGGER_TRIGGER, TRIGGER_CHECK,
                           TRIGGER_APP_HANDLER, TRIGGER_APP_ACTION})

# How a definition fires: a session run, a wake of an existing chat, or an
# app handler's delivery.
SESSION = "session"
WAKE = "wake"
HANDLER = "handler"


@dataclass(frozen=True)
class TaskKind:
    """One definition kind and the facts generic code asks of it."""
    name: str
    clocked: bool         # registers an APScheduler job when it has timing (a trigger task never)
    unscheduled: bool     # accepted by POST /v1/tasks/one-time, which needs no clock
    fires: str            # SESSION | WAKE | HANDLER
    judge: bool           # a judge session: never judged, titled by the task's name
    worker: bool          # a delegate worker: a chat row with lineage, titled by the task's name
    knowledge_rw: bool    # the manager-provenance knowledge-write opt-in at fire (PERMISSIONS.md)
    button_target: bool   # an app's fire_task may point at it (a one-time task deletes itself)
    run_kind: str | None  # the run row's word; None when the kind mints no run row


@dataclass(frozen=True)
class RunKind:
    """One run kind and the fact the re-warm of its chat asks."""
    name: str
    knowledge_rw: bool    # the opt-in a re-warm derives from the run row (today's answers)


KINDS: dict[str, TaskKind] = {k.name: k for k in (
    TaskKind(SCHEDULED, clocked=True, unscheduled=False, fires=SESSION, judge=False, worker=False,
             knowledge_rw=True, button_target=True, run_kind=RUN_SCHEDULED),
    TaskKind(ONE_TIME, clocked=True, unscheduled=True, fires=SESSION, judge=False, worker=False,
             knowledge_rw=True, button_target=False, run_kind=RUN_ONE_TIME),
    TaskKind(TRIGGER, clocked=False, unscheduled=True, fires=SESSION, judge=False, worker=False,
             knowledge_rw=True, button_target=True, run_kind=RUN_TRIGGER),
    TaskKind(CONTINUATION, clocked=True, unscheduled=False, fires=WAKE, judge=False, worker=False,
             knowledge_rw=False, button_target=False, run_kind=None),
    TaskKind(APP, clocked=True, unscheduled=False, fires=HANDLER, judge=False, worker=False,
             knowledge_rw=False, button_target=False, run_kind=RUN_APP),
    TaskKind(DELEGATE, clocked=True, unscheduled=False, fires=SESSION, judge=False, worker=True,
             knowledge_rw=True, button_target=False, run_kind=RUN_DELEGATE),
    TaskKind(CHECK, clocked=True, unscheduled=False, fires=SESSION, judge=True, worker=False,
             knowledge_rw=False, button_target=False, run_kind=RUN_CHECK),
)}
TASK_KINDS = frozenset(KINDS)

# The re-warm's opt-in from the run row's word — today's answers preserved:
# the run column spells ``one-time``, which the old word-set (spelled
# ``one_time``) never matched, and a trigger task's run row was stamped
# ``one-time`` before phase 9 fixed the classifier, so neither a one-time
# nor a trigger task's continued chat opted in. Whether they should follow
# their fire is the operator's call (the plan's "Put to the operator").
RUN_KINDS: dict[str, RunKind] = {k.name: k for k in (
    RunKind(RUN_SCHEDULED, knowledge_rw=True),
    RunKind(RUN_ONE_TIME, knowledge_rw=False),
    RunKind(RUN_TRIGGER, knowledge_rw=False),
    RunKind(RUN_DELEGATE, knowledge_rw=True),
    RunKind(RUN_APP, knowledge_rw=False),
    RunKind(RUN_CHECK, knowledge_rw=False),
)}
RUN_KIND_NAMES = frozenset(RUN_KINDS)

# The kinds whose run row's word comes from the definition's kind alone;
# every other definition (and an empty, pre-derivation value) is classified
# by its timing.
_EXPLICIT = (DELEGATE, APP, CHECK, TRIGGER)


def derive(schedule, interval_seconds) -> str:
    """The timing rule the API, the store and the classifier share: a cron
    or an interval is ``scheduled``, anything else ``one_time``."""
    return SCHEDULED if (schedule or interval_seconds is not None) else ONE_TIME


def of_word(word: str | None) -> TaskKind | None:
    """The kind for a word from the API or a row, ``None`` for a word that
    is not one."""
    return KINDS.get(word or "")


def of(task) -> TaskKind:
    """The definition's kind row. An empty or unknown word reads as the
    timing rule's answer — clocked, a session run, every flag False — which
    is what every branch answered for it before the leaf existed."""
    kind = KINDS.get(getattr(task, "task_type", "") or "")
    if kind is None:
        kind = KINDS[derive(getattr(task, "schedule", None), getattr(task, "interval_seconds", None))]
    return kind


def run_kind_of(task) -> str:
    """The run row's word for a definition — the classifier the runner
    stamps ``task_runs.task_type`` with. The explicit kinds (a delegate, an
    app handler, a judge, a trigger task) by the definition's kind; every
    other word by the timing rule (a ``scheduled`` definition with no clock
    reads ``one-time``, as it always did)."""
    word = getattr(task, "task_type", "") or ""
    if word in _EXPLICIT:
        return KINDS[word].run_kind  # type: ignore[return-value]
    return KINDS[derive(getattr(task, "schedule", None), getattr(task, "interval_seconds", None))].run_kind


def self_removes(task) -> bool:
    """Whether the definition's row goes with the run that ended it: a row
    with no clock (no schedule, no interval) whose kind is not a trigger
    task (a trigger task keeps its row for the next fire)."""
    return (not getattr(task, "schedule", None)
            and getattr(task, "interval_seconds", None) is None
            and (getattr(task, "task_type", "") or "") != TRIGGER)


def spaced(trigger_type: str) -> bool:
    """Whether a fire of this origin waits at the spawn-spacing gate: the
    clock's fires only — Run now has a person waiting and a trigger should
    react at once."""
    return trigger_type == TRIGGER_SCHEDULED


def run_allows_knowledge_rw(run_task_type: str | None) -> bool:
    """The knowledge-write opt-in a re-warm derives from the run row's
    kind; an unknown or empty word fails closed."""
    kind = RUN_KINDS.get(run_task_type or "")
    return bool(kind and kind.knowledge_rw)
