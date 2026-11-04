"""Who is told: an agent-scope task's or
trigger's notifications reach the agent's users at or above its audience
role, platform admins always; a task run's own agent-scope sends carry its
task's audience, read from the run's session, never from the body; a row
below the editor tier carries no chat link; the REST fields, the template
seeder and the updater keep it.

Run: cd proxy && venv/bin/pytest tests/api/test_notification_audience.py -v
"""

from __future__ import annotations

import asyncio
import uuid

import pytest
from fastapi import HTTPException

import config
from api.tasks import tasks as tasks_api
from auth.providers import UserContext
from services.notifications import notification_manager as nm
from storage import database as task_store
from storage.agents import agent_store
from storage.automation import notification_store
from storage.identity import db_users

AG = "audience-agent"
ED, CO, VI, MA = "aud-ed", "aud-co", "aud-vi", "aud-ma"
ADMIN = "user-admin"


@pytest.fixture(autouse=True)
def _agent(monkeypatch, temp_db):
    monkeypatch.setattr(config, "SCHEDULER_MODE", "standalone")
    monkeypatch.setattr(config, "TASK_MAX_ACTIVE_PER_USER", 0)
    monkeypatch.setattr(config, "TASK_MAX_ACTIVE_PER_AGENT", 0)
    agent_store.create_agent(AG, AG, collaborative=True, default_scope="user")
    for sub, role in ((ED, "editor"), (CO, "contributor"), (VI, "viewer"), (MA, "manager")):
        task_store.upsert_user(sub, f"{sub}@x.test", sub, "member")
        db_users.add_user_agent(sub, AG, role, ADMIN)
    nm.reset_audience_cache()
    yield


def _editor(**kw) -> UserContext:
    return UserContext(sub=ED, email="e@x.test", name="Ed", role="member",
                       agents=[AG], agent_roles={AG: "editor"}, **kw)


# ── who the agent's users are, by floor ───────────────────────────────────


def test_the_floor_narrows_the_agents_users_and_keeps_the_admins():
    every = set(nm.resolve_targets("agent", AG))
    assert {ED, CO, VI, MA, ADMIN} <= every
    assert set(nm.resolve_targets("agent", AG, "viewer")) == every
    assert set(nm.resolve_targets("agent", AG, "contributor")) == every - {VI}
    assert set(nm.resolve_targets("agent", AG, "editor")) == every - {VI, CO}
    assert ADMIN in nm.resolve_targets("agent", AG, "manager")
    assert ED not in nm.resolve_targets("agent", AG, "manager")
    # A person-scope or a global send never reads the audience.
    assert nm.resolve_targets("user", ED, "manager") == [ED]


def test_a_row_below_the_editor_tier_carries_no_chat_link(monkeypatch):
    async def _deliver(user_sub, delivery):
        return None
    monkeypatch.setattr(nm, "_deliver_to_user", _deliver)
    deliveries = asyncio.run(nm.fire_notification(
        "t", "b", scope="agent", target=AG, agent_slug=AG, chat_id="task-r-1",
        audience="contributor"))
    by_sub = {d["user_sub"]: d for d in deliveries}
    assert VI not in by_sub
    assert by_sub[CO]["chat_id"] is None and by_sub[CO]["agent_slug"] == AG
    assert by_sub[ED]["chat_id"] == "task-r-1" and by_sub[ADMIN]["chat_id"] == "task-r-1"


# ── a task run's own sends ────────────────────────────────────────────────


def _running_run(task_id: str, session_id: str, audience: str | None) -> None:
    if audience is not None:
        task_store.create_dynamic_task(task_id, AG, "t", "p", "cli", "one_time", None, None, None,
                                       600, ED, scope="agent", notify_audience=audience)
    with task_store.get_conn() as conn:
        conn.execute("INSERT INTO task_runs (id, task_id, agent, trigger_type, status, scope, "
                     "session_id) VALUES (%s, %s, %s, 'manual', 'running', 'agent', %s)",
                     (f"r-{uuid.uuid4().hex[:6]}", task_id, AG, session_id))
        conn.commit()


def test_the_session_names_its_runs_audience():
    _running_run("t-co", "sess-co", "contributor")
    assert task_store.audience_of_session("sess-co") == "contributor"
    _running_run("t-ma", "sess-co", "manager")
    assert task_store.audience_of_session("sess-co") == "manager"   # the narrowest
    _running_run("t-gone", "sess-gone", None)
    assert task_store.audience_of_session("sess-gone") == "editor"  # the row retired
    assert task_store.audience_of_session("sess-chat") is None
    assert task_store.audience_of_session("") is None


def _ended_run_chat(task_id: str, chat_id: str, session_id: str, audience: str | None) -> None:
    """A run's chat after the run ended: its row completed, the chat's
    session the one a later wake or a worker's report resumes."""
    if audience is not None:
        task_store.create_dynamic_task(task_id, AG, "t", "p", "cli", "one_time", None, None, None,
                                       600, ED, scope="agent", notify_audience=audience)
    task_store.create_chat(chat_id, f"task::{AG}", AG)
    task_store.update_chat(chat_id, session_id=session_id)
    with task_store.get_conn() as conn:
        conn.execute("INSERT INTO task_runs (id, task_id, agent, trigger_type, status, scope, "
                     "chat_id, session_id, started_at) VALUES (%s, %s, %s, 'manual', 'completed', "
                     "'agent', %s, %s, NOW()::text)",
                     (f"r-{uuid.uuid4().hex[:6]}", task_id, AG, chat_id, session_id))
        conn.commit()


def test_a_wake_or_a_late_report_in_a_runs_chat_keeps_the_runs_audience(monkeypatch):
    """A continuation fires with no run row and a worker's report can land
    after the run ended: the session holds no live run, so its chat's latest
    run decides (``editor`` when that run's task row is gone); a chat with
    no run at all tells every member."""
    from api.notifications import notifications as napi

    async def _fire(**kw):
        return []
    monkeypatch.setattr(napi.notification_manager, "fire_notification", _fire)
    _ended_run_chat("t-mgr", "task-r-wake", "sess-wake", "manager")
    assert task_store.audience_of_session("sess-wake") == "manager"
    session = UserContext(sub="session:sess-wake", email="", name="", role="agent",
                          is_api_key=True, session_id="sess-wake", agent=AG)
    req = napi.CreateNotificationRequest(title="t", body="b", scope="agent", target=AG,
                                         source="mcp", notification_type="one_time")
    res = asyncio.run(napi.create_notification(req, user=session, x_agent_name=AG))
    assert res["notification"]["audience"] == "manager"
    _ended_run_chat("t-retired", "task-r-late", "sess-late", None)
    assert task_store.audience_of_session("sess-late") == "editor"
    task_store.create_chat("person-chat", ED, AG)
    task_store.update_chat("person-chat", session_id="sess-person")
    assert task_store.audience_of_session("sess-person") is None


def test_a_task_runs_agent_send_carries_its_audience_and_a_chats_tells_everyone(monkeypatch):
    from api.notifications import notifications as napi
    fired: list[dict] = []

    async def _fire(**kw):
        fired.append(kw)
        return []
    monkeypatch.setattr(napi.notification_manager, "fire_notification", _fire)
    _running_run("t-run", "sess-run", "contributor")
    nouser = UserContext(sub="session:sess-run", email="", name="", role="agent",
                         is_api_key=True, session_id="sess-run", agent=AG)
    req = napi.CreateNotificationRequest(title="t", body="b", scope="agent", target=AG,
                                         source="mcp", notification_type="one_time")
    res = asyncio.run(napi.create_notification(req, user=nouser, x_agent_name=AG))
    assert res["notification"]["audience"] == "contributor"
    assert fired[-1]["audience"] == "contributor"
    chat = _editor(is_api_key=True, session_id="sess-chat", agent=AG)
    res = asyncio.run(napi.create_notification(req, user=chat, x_agent_name=AG))
    assert res["notification"]["audience"] == "viewer" and fired[-1]["audience"] == "viewer"


def test_a_scheduled_notification_keeps_its_audience_at_its_fire(monkeypatch):
    fired: list[dict] = []

    async def _fire(**kw):
        fired.append(kw)
        return []
    monkeypatch.setattr(nm, "fire_notification", _fire)
    row = notification_store.create_notification(
        "later", "b", scope="agent", target=AG, notification_type="one_time",
        run_at="2099-01-01T00:00:00", audience="contributor")
    asyncio.run(nm._fire_scheduled_notification(row["id"]))
    assert fired[-1]["audience"] == "contributor"


# ── the REST fields ───────────────────────────────────────────────────────


def _create(u, **kw) -> str:
    req = tasks_api.CreateScheduledTaskRequest(
        name="n", agent=AG, prompt="p", interval_seconds=3600, scope="agent",
        notification_mode="manual", **kw)
    return asyncio.run(tasks_api.create_scheduled_task(req, user=u, x_agent_name=None))["task_id"]


def test_a_new_task_tells_the_editor_tier_unless_it_names_another():
    tid = _create(_editor())
    assert task_store.get_dynamic_task(tid)["notify_audience"] == "editor"
    tid = _create(_editor(), notify_audience="contributor")
    assert task_store.get_dynamic_task(tid)["notify_audience"] == "contributor"
    with pytest.raises(HTTPException) as exc:
        _create(_editor(), notify_audience="admin")
    assert exc.value.status_code == 400


def test_a_task_a_run_creates_keeps_the_runs_audience():
    _running_run("t-parent", "sess-parent", "viewer")
    u = _editor(is_api_key=True, session_id="sess-parent", agent=AG)
    tid = _create(u)
    assert task_store.get_dynamic_task(tid)["notify_audience"] == "viewer"


def test_the_edit_takes_and_checks_the_audience_and_the_name():
    tid = _create(_editor())
    asyncio.run(tasks_api.edit_task(tid, tasks_api.EditTaskRequest(
        notify_audience="manager", name="Renamed"), user=_editor(), x_agent_name=None))
    row = task_store.get_dynamic_task(tid)
    assert row["notify_audience"] == "manager" and row["name"] == "Renamed"
    with pytest.raises(HTTPException) as exc:
        asyncio.run(tasks_api.edit_task(tid, tasks_api.EditTaskRequest(notify_audience="nobody"),
                                        user=_editor(), x_agent_name=None))
    assert exc.value.status_code == 400


def test_a_trigger_takes_its_audience_and_its_notify_carries_it(monkeypatch):
    from api.events import triggers as tapi
    from services.scheduler import trigger_manager
    req = tapi.CreateTriggerRequest(
        name="hook", scope="agent", agent=AG,
        notify=tapi.NotifyConfig(enabled=True, title="t", body="b", target_scope="agent",
                                 audience="contributor"))
    res = asyncio.run(tapi.create_trigger_endpoint(req, user=_editor()))
    row = trigger_store_row(res["trigger"]["id"])
    assert row["notify_audience"] == "contributor"
    plain = asyncio.run(tapi.create_trigger_endpoint(tapi.CreateTriggerRequest(
        name="plain", scope="agent", agent=AG,
        notify=tapi.NotifyConfig(enabled=True, title="t", body="b")), user=_editor()))
    assert trigger_store_row(plain["trigger"]["id"])["notify_audience"] == "editor"
    fired: list[dict] = []

    async def _fire(**kw):
        fired.append(kw)
        return []
    monkeypatch.setattr(nm, "fire_notification", _fire)
    asyncio.run(trigger_manager._fire_inline_notification(row, {}))
    assert fired[-1]["audience"] == "contributor"
    with pytest.raises(HTTPException) as exc:
        asyncio.run(tapi.create_trigger_endpoint(tapi.CreateTriggerRequest(
            name="bad", scope="agent", agent=AG,
            notify=tapi.NotifyConfig(enabled=True, title="t", body="b", audience="everyone")),
            user=_editor()))
    assert exc.value.status_code == 400


def trigger_store_row(trigger_id: str) -> dict:
    from storage.automation import trigger_store
    return trigger_store.get_trigger(trigger_id)


# ── the template ──────────────────────────────────────────────────────────


def test_the_template_names_its_audience_and_the_update_keeps_a_persons_choice():
    from services.community import community_agent_template_store as store
    from services.community.community_agent_updater import _audience_group
    items = store._parse_tasks_json({"tasks": [
        {"slug": "crew", "scope": "agent", "prompt": "p", "schedule": {"type": "cron", "cron": "0 8 * * 1"},
         "audience": "contributor"},
        {"slug": "plain", "scope": "agent", "prompt": "p", "schedule": {"type": "cron", "cron": "0 8 * * 1"}},
    ]})
    assert [i.audience for i in items] == ["contributor", ""]
    assert store._task_from_dict(store._task_to_dict(items[0])).audience == "contributor"
    assert store._task_from_dict(store._task_to_dict(items[1])).audience == ""
    # A record from before 1.9.1 has no audience key at all: None, not "".
    assert store._task_from_dict({k: v for k, v in store._task_to_dict(items[1]).items()
                                  if k != "audience"}).audience is None
    assert _audience_group(items[0]) == {"notify_audience": "contributor"}
    assert _audience_group(items[1]) == {}


@pytest.mark.parametrize("prev_audience,row_audience,moved", [
    (None, "viewer", True),          # a pre-1.9.1 record: the migration's value
    (None, "editor", True),          # a pre-1.9.1 record: a 1.9.1 seed
    ("", "editor", True),            # a 1.9.1 version naming none: the seed's default
    ("", "viewer", False),           # ...and an admin who chose viewer since
    ("manager", "manager", True),    # the version's own value
    ("manager", "editor", False),    # an admin's choice
])
def test_an_update_moves_an_untouched_audience_and_keeps_an_admins(prev_audience, row_audience, moved):
    from types import SimpleNamespace

    from services.community.community_agent_updater import _audience_change
    item, prev = SimpleNamespace(audience="contributor"), SimpleNamespace(audience=prev_audience)
    changes, kept = _audience_change({"notify_audience": row_audience}, item, prev)
    assert (changes == {"notify_audience": "contributor"}, kept) == (moved, not moved)


def test_a_template_triggers_audience_reaches_its_paired_task(monkeypatch):
    """The updater's trigger loop carries a new version's audience onto the
    paired task row, as the task loop does, and keeps an admin's choice."""
    from types import SimpleNamespace

    from services.community import community_agent_updater as upd
    from storage.automation import db_tasks
    updates: list = []
    monkeypatch.setattr(db_tasks, "update_dynamic_task", lambda rid, ch: updates.append((rid, ch)))
    row = {"id": "t-pair", "prompt": "p", "notify_audience": "editor"}
    monkeypatch.setattr(db_tasks, "find_template_task", lambda agent, key, sub=None: dict(row))
    trig_new = SimpleNamespace(slug="hook", scope="agent", prompt="p", description="Hook",
                               audience="contributor")
    trig_old = SimpleNamespace(slug="hook", scope="agent", prompt="p", description="Hook", audience="")
    report = {"kept": []}
    run = SimpleNamespace(apply=True, agent=AG, by_sub=ED, report=report,
                          new=SimpleNamespace(tasks=[], triggers=[trig_new]),
                          replaced=lambda what: report.setdefault("replaced", []).append(what),
                          same=lambda: report.setdefault("same", 0), added=lambda w: None,
                          removed=lambda w: None)
    upd._merge_trigger_rows(run, trig_new, trig_old, [row], "the trigger 'hook'")
    assert updates == [("t-pair", {"notify_audience": "contributor"})]



def test_a_task_run_never_widens_its_own_audience():
    """The run's audience comes from its token: from a run's session a task
    create or edit and a trigger create or edit may name the run's audience
    or a narrower one, never a wider one; a trigger it creates without one
    takes the run's. A person's own session is not held to it."""
    from api.events import triggers as tapi
    _running_run("t-run-floor", "sess-floor", "editor")
    run = _editor(is_api_key=True, session_id="sess-floor", agent=AG)
    with pytest.raises(HTTPException) as exc:
        _create(run, notify_audience="viewer")
    assert exc.value.status_code == 400 and "editor role and above" in exc.value.detail
    tid = _create(run, notify_audience="manager")
    with pytest.raises(HTTPException):
        asyncio.run(tasks_api.edit_task(tid, tasks_api.EditTaskRequest(notify_audience="contributor"),
                                        user=run, x_agent_name=None))
    with pytest.raises(HTTPException):
        asyncio.run(tasks_api.edit_task("t-run-floor", tasks_api.EditTaskRequest(
            notify_audience="viewer"), user=run, x_agent_name=None))
    with pytest.raises(HTTPException):
        asyncio.run(tapi.create_trigger_endpoint(tapi.CreateTriggerRequest(
            name="wide", scope="agent", agent=AG,
            notify=tapi.NotifyConfig(enabled=True, title="t", body="b", audience="viewer")), user=run))
    made = asyncio.run(tapi.create_trigger_endpoint(tapi.CreateTriggerRequest(
        name="inherits", scope="agent", agent=AG,
        notify=tapi.NotifyConfig(enabled=True, title="t", body="b")), user=run))
    assert trigger_store_row(made["trigger"]["id"])["notify_audience"] == "editor"
    with pytest.raises(HTTPException) as exc:
        asyncio.run(tapi.edit_trigger_endpoint(made["trigger"]["id"],
                                               tapi.EditTriggerRequest(notify_audience=None), user=run))
    assert exc.value.status_code == 400
    # A person's own session (no run on it) names what it likes.
    assert task_store.get_dynamic_task(_create(_editor(), notify_audience="viewer"))["notify_audience"] == "viewer"
