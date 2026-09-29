"""The caps on active tasks (``api/tasks/task_quota.py``): one person holds
at most TASK_MAX_ACTIVE_PER_USER active scheduled or one-time tasks of their
own across agents, one agent at most TASK_MAX_ACTIVE_PER_AGENT of both
scopes; a create, a resume and a timing edit that would pass a cap answer
429 with the count and the limit. Admins and the master key are exempt from
the per-person cap, 0 turns a cap off, seeded rows count for nobody.

Run: cd proxy && venv/bin/pytest tests/tasks/test_task_caps.py -v
"""

from __future__ import annotations

import asyncio

import pytest
from fastapi import HTTPException

import config
from api.tasks import tasks as tasks_api
from api.tasks import task_quota
from auth.providers import UserContext
from services.scheduler import task_kinds
from storage import database as task_store
from storage.agents import agent_store

PM, ED, ADMIN = "pm-1", "ed-1", "user-admin"


@pytest.fixture(autouse=True)
def _standalone(monkeypatch, temp_db):
    # No APScheduler side effects; the caps read the rows, not the jobs.
    monkeypatch.setattr(config, "SCHEDULER_MODE", "standalone")
    monkeypatch.setattr(config, "TASK_MAX_ACTIVE_PER_USER", 0)
    monkeypatch.setattr(config, "TASK_MAX_ACTIVE_PER_AGENT", 0)
    for slug in ("proj-a", "proj-b"):
        agent_store.create_agent(slug, slug, collaborative=True, default_scope="user")
    task_store.upsert_user(PM, "pm@x.test", "PM", "member")
    task_store.upsert_user(ED, "ed@x.test", "Ed", "member")
    task_store.set_user_agent_role(PM, "proj-a", "contributor")
    task_store.set_user_agent_role(PM, "proj-b", "contributor")
    task_store.set_user_agent_role(ED, "proj-a", "editor")
    yield


def _person(sub: str, role_map: dict[str, str], platform: str = "member") -> UserContext:
    return UserContext(sub=sub, email=f"{sub}@x.test", name=sub, role=platform,
                       agents=list(role_map), agent_roles=dict(role_map))


def _pm():
    return _person(PM, {"proj-a": "contributor", "proj-b": "contributor"})


def _editor():
    return _person(ED, {"proj-a": "editor"})


def _admin():
    return _person(ADMIN, {}, platform="admin")


def _create(u, *, agent="proj-a", scope="user", name="t", interval=60):
    req = tasks_api.CreateScheduledTaskRequest(
        name=name, agent=agent, prompt="p", interval_seconds=interval, scope=scope,
        notification_mode="none")
    return asyncio.run(tasks_api.create_scheduled_task(req, user=u, x_agent_name=None))["task_id"]


def _one_time(u, *, task_type=None, agent="proj-a", scope="user"):
    req = tasks_api.CreateOneTimeTaskRequest(
        name="o", agent=agent, prompt="p", scope=scope, notification_mode="none",
        task_type=task_type, **({} if task_type else {"delay_seconds": 3600}))
    return asyncio.run(tasks_api.create_one_time_task(req, user=u, x_agent_name=None))["task_id"]


def _refused(fn, *args, **kwargs) -> str:
    with pytest.raises(HTTPException) as ei:
        fn(*args, **kwargs)
    assert ei.value.status_code == 429
    return ei.value.detail


def test_a_person_holds_at_most_the_active_tasks_cap(monkeypatch):
    monkeypatch.setattr(config, "TASK_MAX_ACTIVE_PER_USER", 2)
    _create(_pm(), agent="proj-a")
    _create(_pm(), agent="proj-b")            # across agents
    detail = _refused(_create, _pm(), agent="proj-a")
    assert "2 active scheduled tasks" in detail and "the limit is 2" in detail
    _refused(_one_time, _pm())                # a one-time row counts too
    _one_time(_pm(), task_type=task_kinds.TRIGGER)   # no clock: not counted
    assert task_store.count_active_clocked_tasks(created_by=PM) == 2


def test_agent_scope_rows_count_per_agent_only(monkeypatch):
    monkeypatch.setattr(config, "TASK_MAX_ACTIVE_PER_USER", 2)
    monkeypatch.setattr(config, "TASK_MAX_ACTIVE_PER_AGENT", 3)
    # An editor's agent-scope rows never count toward the person's cap...
    for i in range(3):
        _create(_editor(), scope="agent", name=f"a{i}")
    # ...but they fill the agent's, for both scopes and every creator.
    _refused(_create, _editor(), scope="agent")
    detail = _refused(_create, _pm(), agent="proj-a")
    assert "This agent already has 3 active scheduled tasks" in detail
    _create(_pm(), agent="proj-b")            # another agent is free


def test_a_trigger_task_given_an_interval_counts(monkeypatch):
    monkeypatch.setattr(config, "TASK_MAX_ACTIVE_PER_USER", 1)
    tid = _one_time(_pm(), task_type=task_kinds.TRIGGER)
    _create(_pm())                            # the one allowed row
    req = tasks_api.EditTaskRequest(interval_seconds=120)
    _refused(lambda: asyncio.run(tasks_api._edit_task_impl(tid, req, _pm(), None)))
    assert task_store.get_dynamic_task(tid)["task_type"] == task_kinds.TRIGGER


def test_a_resume_past_the_cap_is_refused(monkeypatch):
    monkeypatch.setattr(config, "TASK_MAX_ACTIVE_PER_USER", 2)
    first = _create(_pm(), name="first")
    _create(_pm(), name="second")
    asyncio.run(tasks_api.pause_task(first, user=_pm(), x_agent_name=None))
    _create(_pm(), name="third")              # a paused row does not count
    _refused(lambda: asyncio.run(tasks_api.resume_task(first, user=_pm(), x_agent_name=None)))
    assert task_store.get_dynamic_task(first)["enabled"] is False


def test_an_admin_the_master_key_and_a_zero_cap(monkeypatch):
    monkeypatch.setattr(config, "TASK_MAX_ACTIVE_PER_USER", 1)
    monkeypatch.setattr(config, "TASK_MAX_ACTIVE_PER_AGENT", 2)
    for i in range(2):
        _create(_admin(), scope="user", name=f"adm{i}")      # no per-person cap for an admin
    _refused(_create, _admin(), scope="user")                # the agent cap holds
    svc = UserContext(sub="api-key", email="", name="", role="admin", is_api_key=True)
    req = tasks_api.CreateScheduledTaskRequest(
        name="svc", agent="proj-a", prompt="p", interval_seconds=60, scope="agent",
        notification_mode="none")
    asyncio.run(tasks_api.create_scheduled_task(req, user=svc, x_agent_name="proj-a"))
    monkeypatch.setattr(config, "TASK_MAX_ACTIVE_PER_AGENT", 0)
    _create(_pm(), name="free")


def test_seeded_rows_are_not_counted(monkeypatch):
    monkeypatch.setattr(config, "TASK_MAX_ACTIVE_PER_USER", 1)
    task_store.create_dynamic_task(
        "dyn-seeded", "proj-a", "seeded", "p", "cli", task_kinds.SCHEDULED, "0 3 * * *",
        None, None, 600, PM, scope="user", community_template="tpl",
        community_template_item_slug="nightly")
    assert task_quota.counted_task(task_store.get_dynamic_task("dyn-seeded")) is False
    _create(_pm())                            # the person's one row is still free


def test_a_seeded_trigger_task_given_a_clock_becomes_the_editors_and_counts(monkeypatch):
    """A bundle the person imported seeds trigger tasks under their name;
    the seeded rows count for nobody while the clock is the installer's. A
    clock given by an edit is the editor's: the row stops being the
    template's and meets both caps, on the edit and on a later resume."""
    monkeypatch.setattr(config, "TASK_MAX_ACTIVE_PER_USER", 1)
    for i in range(4):
        task_store.create_dynamic_task(
            f"dyn-seed-{i}", "proj-a", f"seeded {i}", "p", "cli", task_kinds.TRIGGER, None,
            None, None, 600, PM, scope="user", community_template="bundle:pack",
            community_template_item_slug=f"pack__t{i}")
    every_minute = tasks_api.EditTaskRequest(interval_seconds=60)

    def _edit(tid: str) -> None:
        asyncio.run(tasks_api._edit_task_impl(tid, every_minute, _pm(), None))

    _edit("dyn-seed-0")
    row = task_store.get_dynamic_task("dyn-seed-0")
    assert row["task_type"] == task_kinds.SCHEDULED and row["community_template"] is None
    # The template updater still finds the row it seeded.
    assert row["community_template_item_slug"] == "pack__t0"
    assert task_store.count_active_clocked_tasks(created_by=PM, scope="user") == 1
    detail = _refused(_edit, "dyn-seed-1")
    assert "1 active scheduled task" in detail and "the limit is 1" in detail
    assert task_store.get_dynamic_task("dyn-seed-1")["task_type"] == task_kinds.TRIGGER
    # Paused first, then given a clock: the resume is where it would count.
    asyncio.run(tasks_api.pause_task("dyn-seed-2", user=_pm(), x_agent_name=None))
    _edit("dyn-seed-2")
    assert task_store.get_dynamic_task("dyn-seed-2")["community_template"] is None
    _refused(lambda: asyncio.run(tasks_api.resume_task("dyn-seed-2", user=_pm(), x_agent_name=None)))
    # The agent's cap holds the same rows.
    monkeypatch.setattr(config, "TASK_MAX_ACTIVE_PER_USER", 0)
    monkeypatch.setattr(config, "TASK_MAX_ACTIVE_PER_AGENT", 1)
    assert "This agent already has 1 active scheduled task" in _refused(_edit, "dyn-seed-3")


def test_a_timing_edit_on_a_continuation_is_refused():
    task_store.create_dynamic_task(
        "dyn-cont", "proj-a", "c", "p", "cli", task_kinds.CONTINUATION, None,
        "2099-01-01T10:00:00", None, 600, PM, scope="user", target_chat_id="chat-x")
    req = tasks_api.EditTaskRequest(interval_seconds=600)
    with pytest.raises(HTTPException) as ei:
        asyncio.run(tasks_api._edit_task_impl("dyn-cont", req, _pm(), None))
    assert ei.value.status_code == 400
    assert task_store.get_dynamic_task("dyn-cont")["task_type"] == task_kinds.CONTINUATION
    # A rename is still an edit.
    asyncio.run(tasks_api._edit_task_impl("dyn-cont", tasks_api.EditTaskRequest(name="c2"), _pm(), None))
    assert task_store.get_dynamic_task("dyn-cont")["name"] == "c2"
