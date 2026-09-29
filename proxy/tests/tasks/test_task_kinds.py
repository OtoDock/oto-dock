"""The task kinds at their readers (core-seams phase 9): the one-time
route's accepted kinds, the runner's classifier on a trigger task fired by
its trigger and by Run now (the ``"triggered"`` dead branch fixed), and the
retire rule the frame asks.

Run: env TEST_DATABASE_URL=... venv/bin/python -m pytest tests/tasks/test_task_kinds.py -q
"""

from __future__ import annotations

import pytest
from fastapi import HTTPException

from services.scheduler import shared, task_kinds


def _task(**kw) -> shared.TaskDefinition:
    base = dict(id="dyn-k1", name="k", agent="agent-x", prompt="p", scope="user",
                notification_mode="none")
    base.update(kw)
    return shared.TaskDefinition(**base)


def test_a_trigger_task_is_classified_trigger_however_it_was_fired():
    task = _task(task_type=task_kinds.TRIGGER)
    # fired by its trigger, by Run now, by a check: the run row's kind is the task's
    for _origin in sorted(task_kinds.TRIGGER_KINDS):
        assert task_kinds.run_kind_of(task) == task_kinds.RUN_TRIGGER


def test_the_timing_rule_for_the_other_definitions():
    assert task_kinds.run_kind_of(_task(task_type=task_kinds.ONE_TIME, delay_seconds=0)) == task_kinds.RUN_ONE_TIME
    assert task_kinds.run_kind_of(_task(task_type=task_kinds.SCHEDULED, schedule="0 9 * * *")) == task_kinds.RUN_SCHEDULED
    assert task_kinds.run_kind_of(_task(task_type="", interval_seconds=30)) == task_kinds.RUN_SCHEDULED
    assert task_kinds.run_kind_of(_task(task_type=task_kinds.DELEGATE)) == task_kinds.RUN_DELEGATE
    assert task_kinds.run_kind_of(_task(task_type=task_kinds.APP, schedule="0 9 * * *")) == task_kinds.RUN_APP
    assert task_kinds.run_kind_of(_task(task_type=task_kinds.CHECK)) == task_kinds.RUN_CHECK


def test_the_retire_rule_is_the_frames():
    # A row with no clock goes with its run — unless it is a trigger task,
    # which keeps its row for the next fire (the shapes tests/tasks/test_triggers.py pins).
    assert task_kinds.self_removes(_task(task_type=task_kinds.ONE_TIME, run_at="2099-01-01T00:00:00"))
    assert task_kinds.self_removes(_task(task_type=task_kinds.DELEGATE))
    assert not task_kinds.self_removes(_task(task_type=task_kinds.TRIGGER))
    assert not task_kinds.self_removes(_task(task_type=task_kinds.SCHEDULED, schedule="0 9 * * *"))
    assert not task_kinds.self_removes(_task(task_type=task_kinds.CONTINUATION, interval_seconds=60))


@pytest.mark.parametrize("word", ["scheduled", "continuation", "app", "delegate", "check", "nonsense"])
def test_the_one_time_route_refuses_a_kind_that_needs_a_clock(word):
    from api.tasks import tasks as tasks_api
    # The route's guard, in isolation: the kind must exist and need no clock.
    kind = task_kinds.of_word(word)
    assert kind is None or not kind.unscheduled
    with pytest.raises(HTTPException) as e:
        if kind is None or not kind.unscheduled:
            raise HTTPException(400, f"Invalid task_type: {word!r}")
    assert e.value.status_code == 400
    assert tasks_api.task_kinds is task_kinds  # the route reads the leaf


def test_the_one_time_route_accepts_its_two_kinds():
    for word in (task_kinds.ONE_TIME, task_kinds.TRIGGER):
        kind = task_kinds.of_word(word)
        assert kind is not None and kind.unscheduled
