"""What the agent does on its own behalf takes the editor tier to see:
below it a contributor or a viewer sees their own tasks, runs, triggers,
notifications and chats and nothing of the agent's own work, on every read
surface; an editor, a manager, an admin, the master key and the agent's own
no-user session see both. A Shared-only agent's human pool chats stay
readable to its members.

Run: cd proxy && venv/bin/pytest tests/api/test_shared_automations_below_editor.py -v
"""

from __future__ import annotations

import asyncio

import pytest
from fastapi import HTTPException

from auth.providers import UserContext
from storage import database as task_store
from storage.agents import agent_store
from storage.automation import notification_store, trigger_store

AG = "shared-agent"
OWNER = "user-viewer"      # a seeded member: the person whose own rows these are
OTHER = "user-viewer2"     # another seeded member


def _person(role: str, *, session: bool = False) -> UserContext:
    return UserContext(
        sub=OWNER, email="p@t.com", name="Person", role="member", agents=[AG],
        agent_roles={AG: role}, is_api_key=session,
        session_id="sess-1" if session else "", agent=AG if session else "")


def _admin() -> UserContext:
    return UserContext(sub="user-admin", email="a@t.com", name="Admin", role="admin")


def _master() -> UserContext:
    return UserContext(sub="api-key", email="", name="", role="admin", is_api_key=True)


def _nouser() -> UserContext:
    return UserContext(sub="session:s-1", email="", name="", role="agent",
                       is_api_key=True, session_id="s-1", agent=AG)


BELOW = ["contributor", "viewer"]
AT_TIER = ["editor", "manager"]


# ── the predicate ─────────────────────────────────────────────────────────


class TestThePredicate:
    def test_the_tier_and_the_principals(self):
        for role in BELOW:
            assert not _person(role).sees_agent_automations(AG)
            assert not _person(role, session=True).sees_agent_automations(AG)
        for role in AT_TIER:
            assert _person(role).sees_agent_automations(AG)
        assert _admin().sees_agent_automations(AG)
        assert _master().sees_agent_automations(AG)
        assert _nouser().sees_agent_automations(AG)
        # A person's API key answers its owner's per-agent role.
        for role, sees in (("viewer", False), ("contributor", False), ("editor", True)):
            key = UserContext(sub=OWNER, email="p@t.com", name="Person", role="member", agents=[AG],
                              agent_roles={AG: role}, is_api_key=True)
            assert key.sees_agent_automations(AG) is sees

    def test_an_external_caller_is_not_the_agent(self):
        ext = UserContext(sub="session:s-2", email="", name="", role="agent", is_api_key=True,
                          session_id="s-2", agent=AG, external_claim="phone:+30")
        assert ext.is_external and not ext.sees_agent_automations(AG)

    def test_automation_agents_for_a_sql_filter(self):
        from auth.providers import automation_agents
        assert automation_agents(_admin()) is None
        assert automation_agents(_master()) is None
        assert automation_agents(_nouser()) is None
        assert automation_agents(_person("editor")) == [AG]
        assert automation_agents(_person("contributor")) == []


# ── tasks ─────────────────────────────────────────────────────────────────


class _Def:
    def __init__(self, id, agent, scope, created_by):
        self.id, self.agent = id, agent
        self.scope, self.created_by = scope, created_by
        self.use_persistent = False
        self.enabled = True
        self.transferred_from = ""
        self.task_type = "scheduled"

    def model_dump(self):
        return {"id": self.id, "agent": self.agent}


def _seed_defs(monkeypatch):
    from api.tasks import tasks as tasks_mod
    defs = [_Def("t-ag", AG, "agent", "user-admin"), _Def("t-own", AG, "user", OWNER),
            _Def("t-other", AG, "user", OTHER)]
    monkeypatch.setattr(tasks_mod.scheduler, "get_all_task_definitions", lambda: defs)
    monkeypatch.setattr(tasks_mod.scheduler, "get_scheduled_jobs",
                        lambda: [{"task_id": d.id} for d in defs])


class TestTaskListing:
    @pytest.mark.parametrize("role", BELOW)
    @pytest.mark.parametrize("session", [False, True])
    def test_below_the_tier_lists_only_its_own(self, temp_db, monkeypatch, role, session):
        from api.tasks.tasks import list_schedules, list_tasks
        _seed_defs(monkeypatch)
        res = asyncio.run(list_tasks(agent=None, audit=False, user=_person(role, session=session)))
        assert {t["id"] for t in res["tasks"]} == {"t-own"}
        jobs = asyncio.run(list_schedules(user=_person(role, session=session)))
        assert {j["task_id"] for j in jobs["schedules"]} == {"t-own"}

    @pytest.mark.parametrize("role", AT_TIER)
    def test_the_tier_lists_the_agents_own_too(self, temp_db, monkeypatch, role):
        from api.tasks.tasks import list_schedules, list_tasks
        _seed_defs(monkeypatch)
        res = asyncio.run(list_tasks(agent=None, audit=False, user=_person(role)))
        assert {t["id"] for t in res["tasks"]} == {"t-ag", "t-own"}
        jobs = asyncio.run(list_schedules(user=_person(role)))
        assert {j["task_id"] for j in jobs["schedules"]} == {"t-ag", "t-own"}

    def test_the_agent_itself_lists_its_own(self, temp_db, monkeypatch):
        from api.tasks.tasks import list_tasks
        _seed_defs(monkeypatch)
        res = asyncio.run(list_tasks(agent=None, audit=False, user=_nouser()))
        assert {t["id"] for t in res["tasks"]} == {"t-ag"}

    def test_the_stats_are_the_admins(self, temp_db):
        from api.tasks.tasks import get_stats
        with pytest.raises(HTTPException) as exc:
            asyncio.run(get_stats(user=_person("manager")))
        assert exc.value.status_code == 403
        assert "running_tasks" in asyncio.run(get_stats(user=_admin()))


def _seed_runs():
    agent_store.create_agent(AG, "Shared", created_by="user-admin")
    with task_store.get_conn() as conn:
        for rid, by, scope, chat in [("r-ag", None, "agent", "task-r-ag"),
                                     ("r-own", OWNER, "user", "task-r-own"),
                                     ("r-other", OTHER, "user", "task-r-other")]:
            conn.execute(
                "INSERT INTO task_runs (id, task_id, agent, trigger_type, status, created_by, "
                "scope, started_at, chat_id) VALUES (%s, 't', %s, 'manual', 'completed', %s, %s, "
                "NOW()::text, %s)", (rid, AG, by, scope, chat))
        conn.commit()


class TestRuns:
    @pytest.mark.parametrize("role", BELOW)
    def test_below_the_tier(self, temp_db, role):
        from api.tasks.tasks import _check_run_access, list_runs
        _seed_runs()
        res = asyncio.run(list_runs(agent=None, status=None, task_id=None, session_id=None,
                                    created_by=None, audit=False, include_delegates=False,
                                    limit=50, offset=0, user=_person(role)))
        assert {r["id"] for r in res["runs"]} == {"r-own"} and res["total"] == 1
        with pytest.raises(HTTPException) as exc:
            _check_run_access(task_store.get_run("r-ag"), _person(role))
        assert exc.value.status_code == 403
        _check_run_access(task_store.get_run("r-own"), _person(role))

    @pytest.mark.parametrize("role", AT_TIER)
    def test_at_the_tier(self, temp_db, role):
        from api.tasks.tasks import _check_run_access, list_runs
        _seed_runs()
        res = asyncio.run(list_runs(agent=AG, status=None, task_id=None, session_id=None,
                                    created_by=None, audit=False, include_delegates=False,
                                    limit=50, offset=0, user=_person(role)))
        assert {r["id"] for r in res["runs"]} == {"r-ag", "r-own"} and res["total"] == 2
        _check_run_access(task_store.get_run("r-ag"), _person(role))

    def test_the_heat_counts_the_agents_runs_only_at_the_tier(self, temp_db):
        _seed_runs()
        since = "2000-01-01T00:00:00"
        assert task_store.count_recent_runs_by_agent(since, OWNER, []) == {AG: 1}
        assert task_store.count_recent_runs_by_agent(since, OWNER, [AG]) == {AG: 2}
        assert task_store.count_recent_runs_by_agent(since, OWNER) == {AG: 2}


# ── triggers and notifications ────────────────────────────────────────────


class TestTriggersAndNotifications:
    def _seed(self):
        agent_store.create_agent(AG, "Shared", created_by="user-admin")
        trigger_store.create_trigger(slug="tr-ag", name="ag", scope="agent", agent=AG,
                                     created_by="user-admin")
        trigger_store.create_trigger(slug="tr-own", name="own", scope="user", agent=AG,
                                     created_by=OWNER)
        notification_store.create_notification("n-ag", "b", scope="agent", target=AG,
                                               created_by="user-admin")
        notification_store.create_notification("n-own", "b", scope="user", target=OWNER,
                                               created_by=OWNER)

    def _views(self, ctx):
        from api.events.triggers import list_triggers_endpoint
        from api.notifications.notifications import list_notifications
        triggers = asyncio.run(list_triggers_endpoint(agent=AG, scope=None, audit=False, user=ctx))
        notifs = asyncio.run(list_notifications(scope=None, source=None, agent=AG, audit=False,
                                                view="definitions", user=ctx, x_agent_name=None))
        return ({t["slug"] for t in triggers["triggers"]},
                {n["title"] for n in notifs["notifications"]})

    @pytest.mark.parametrize("role", BELOW)
    def test_below_the_tier(self, temp_db, role):
        from api.events.triggers import _can_view_trigger
        self._seed()
        assert self._views(_person(role)) == ({"tr-own"}, {"n-own"})
        assert self._views(_person(role, session=True)) == ({"tr-own"}, {"n-own"})
        row = next(r for r in trigger_store.list_triggers(agent=AG) if r["slug"] == "tr-ag")
        assert not _can_view_trigger(row, _person(role))

    @pytest.mark.parametrize("role", AT_TIER)
    def test_at_the_tier(self, temp_db, role):
        self._seed()
        assert self._views(_person(role)) == ({"tr-ag", "tr-own"}, {"n-ag", "n-own"})

    def test_the_grid_counts(self, temp_db):
        self._seed()
        assert trigger_store.count_user_visible_triggers_by_agent(OWNER, []) == {AG: 1}
        assert trigger_store.count_user_visible_triggers_by_agent(OWNER, [AG]) == {AG: 2}


# ── chats ─────────────────────────────────────────────────────────────────


def _chat(chat_id: str, owner: str, *, source_type: str = "chat") -> dict:
    return task_store.create_chat(chat_id, owner, AG, source_type=source_type)


class TestChats:
    @pytest.mark.parametrize("role", BELOW)
    def test_a_run_chat_and_a_pool_worker_are_closed_below_the_tier(self, temp_db, role):
        from core.session.visibility import can_access_chat
        _seed_runs()
        run_chat = _chat("task-r-ag", f"task::{AG}", source_type="task")
        own_chat = _chat("task-r-own", OWNER, source_type="task")
        worker = _chat("worker-1", f"agent::{AG}")
        pool = _chat("pool-1", f"agent::{AG}")
        with task_store.get_conn() as conn:
            conn.execute("INSERT INTO task_runs (id, task_id, agent, trigger_type, status, scope, "
                         "chat_id) VALUES ('r-w', 't', %s, 'delegate', 'completed', 'agent', "
                         "'worker-1')", (AG,))
            conn.commit()
        person = _person(role)
        assert not can_access_chat(person, run_chat)
        assert can_access_chat(person, own_chat)
        assert not can_access_chat(person, worker)
        assert can_access_chat(person, pool)
        for at in AT_TIER:
            assert can_access_chat(_person(at), run_chat) and can_access_chat(_person(at), worker)

    @pytest.mark.parametrize("role", BELOW)
    def test_the_listings_below_the_tier(self, temp_db, role):
        from api.agents.chats import _list_chats_sync
        _seed_runs()
        _chat("task-r-ag", f"task::{AG}", source_type="task")
        _chat("task-r-own", OWNER, source_type="task")
        _chat("worker-1", f"agent::{AG}")
        _chat("pool-1", f"agent::{AG}")
        with task_store.get_conn() as conn:
            conn.execute("INSERT INTO task_runs (id, task_id, agent, trigger_type, status, scope, "
                         "chat_id) VALUES ('r-w', 't', %s, 'delegate', 'completed', 'agent', "
                         "'worker-1')", (AG,))
            conn.execute("UPDATE agents SET collaborative=FALSE, default_scope='agent' WHERE slug=%s",
                         (AG,))
            conn.commit()
        agent_store._invalidate_cache()
        tasks = _list_chats_sync(_person(role), AG, "tasks", 50)
        assert {c["id"] for c in tasks} == {"task-r-own"}
        chats = _list_chats_sync(_person(role), AG, "chats", 50)
        assert {c["id"] for c in chats} == {"pool-1"}
        editor_chats = _list_chats_sync(_person("editor"), AG, "chats", 50)
        assert {c["id"] for c in editor_chats} == {"pool-1", "worker-1"}
        editor_tasks = _list_chats_sync(_person("editor"), AG, "tasks", 50)
        assert {c["id"] for c in editor_tasks} == {"task-r-ag", "task-r-own"}

    def test_active_now_hides_the_agents_own_runs(self, temp_db):
        from api.agents.chats import _active_rows
        _seed_runs()
        _chat("task-r-ag", f"task::{AG}", source_type="task")
        _chat("task-r-own", OWNER, source_type="task")
        since = "2000-01-01T00:00:00"
        below = _active_rows(_person("contributor"), ["task-r-ag", "task-r-own"], [], since)
        assert {r["id"] for r in below} == {"task-r-own"}
        at = _active_rows(_person("editor"), ["task-r-ag", "task-r-own"], [], since)
        assert {r["id"] for r in at} == {"task-r-ag", "task-r-own"}


# ── the prompt's parallel sessions and the verdicts ───────────────────────


class TestThePreludeAndTheVerdicts:
    def test_the_prelude_drops_the_agents_tasks_below_the_tier(self, temp_db, monkeypatch):
        from core.session import sibling_awareness
        from storage.identity import db_users
        db_users.add_user_agent(OWNER, AG, "contributor", "user-admin")
        snap = {"lanes": {}, "tasks": [
            {"agent": AG, "scope": "agent", "task_id": "dyn-ag", "name": "Agent job", "chat_id": ""},
            {"agent": AG, "scope": "user", "task_id": "dyn-own", "name": "Mine",
             "created_by": OWNER, "chat_id": ""},
        ]}
        monkeypatch.setattr(sibling_awareness, "_get_snapshot_sync", lambda: snap)
        _siblings, tasks = sibling_awareness._gather_sync(AG, OWNER, own_chat_id="")
        assert [t[1] for t in tasks] == ["dyn-own"]
        _siblings, tasks = sibling_awareness._gather_sync(AG, f"agent::{AG}", own_chat_id="")
        assert [t[1] for t in tasks] == ["dyn-ag"]

    def test_the_verdicts_below_the_tier_are_the_persons_own(self, temp_db):
        from storage.checks import db_checks
        rows = db_checks.list_verdicts(AG, user_sub=OWNER, agent_scope=False)
        assert all(r.get("user_sub") == OWNER for r in rows)
