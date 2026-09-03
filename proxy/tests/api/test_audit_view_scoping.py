"""Agent-settings (user-view) vs admin-audit (full) scoping for tasks / triggers
/ notifications / subscriptions / task-history.

The rule: an `agent`-scoped item is shared; a user's user-scoped item is private
to them. The admin audit surface (``audit=true``, admin only) sees every user's
items; everyone else — INCLUDING an admin on an agent's settings tab — sees the
user-view (own user-scoped + agent-scoped). Session JWTs are api-key-shaped but
carry a real (or no-user) identity — only the MASTER key (`is_service`) is
unfiltered (H1/H2 for tasks; the C3 hardening extended the same re-key to
triggers, notifications, and subscriptions).

Run: cd proxy && venv/bin/pytest tests/api/test_audit_view_scoping.py -v
"""

from __future__ import annotations

import asyncio
import sys

import pytest
from fastapi import HTTPException

from tests._paths import PROXY_DIR
_proxy_root = str(PROXY_DIR)
if _proxy_root not in sys.path:
    sys.path.insert(0, _proxy_root)

from auth.providers import UserContext  # noqa: E402
from storage import agent_store  # noqa: E402
from storage import database as task_store  # noqa: E402
from storage import notification_store  # noqa: E402
from storage import trigger_store  # noqa: E402
from storage import webhook_subscription_store  # noqa: E402

AG = "shared-agent"
OTHER = "other-agent"
M = "user-manager"
V = "user-viewer"


def _admin() -> UserContext:
    return UserContext(sub="user-admin", email="a@t.com", name="Admin", role="admin")


def _viewer() -> UserContext:
    return UserContext(sub=V, email="v@t.com", name="Viewer", role="member", agents=[AG])


def _apikey() -> UserContext:
    return UserContext(sub="api-key", email="", name="", role="admin", is_api_key=True)


def _user_session() -> UserContext:
    """User-backed per-session JWT: is_api_key=True + the real user's identity."""
    return UserContext(
        sub=V, email="v@t.com", name="Viewer", role="member",
        agents=[AG], agent_roles={AG: "viewer"},
        is_api_key=True, session_id="sess-1", agent=AG)


def _nouser_session(agent: str = AG) -> UserContext:
    """No-user AGENT_SESSION JWT (phone / trigger / agent-scope task)."""
    return UserContext(
        sub="session:s-1", email="", name="", role="agent",
        is_api_key=True, session_id="s-1", agent=agent)


class TestScopeFilterSub:
    """The pure predicate behind Task History scoping."""

    def test_logic(self):
        from api.tasks.tasks import _scope_filter_sub
        admin, member, api = _admin(), _viewer(), _apikey()
        # API key → unfiltered.
        assert _scope_filter_sub(api, AG, audit=False) is None
        # Admin AUDIT (even with an agent filter) → unfiltered (full audit).
        assert _scope_filter_sub(admin, None, audit=True) is None
        assert _scope_filter_sub(admin, AG, audit=True) is None
        # Admin on an agent's settings tab (no audit) → user-view.
        assert _scope_filter_sub(admin, AG, audit=False) == "user-admin"
        assert _scope_filter_sub(admin, None, audit=False) == "user-admin"
        # Regular user → always user-view.
        assert _scope_filter_sub(member, AG, audit=False) == V


class TestTriggersEndpointScoping:
    def _seed(self):
        agent_store.create_agent(AG, "Shared", created_by="user-admin")
        trigger_store.create_trigger(slug="tr-ag", name="ag", scope="agent", agent=AG, created_by="user-admin")
        trigger_store.create_trigger(slug="tr-m", name="m", scope="user", agent=AG, created_by=M)
        trigger_store.create_trigger(slug="tr-v", name="v", scope="user", agent=AG, created_by=V)

    def _slugs(self, ctx, *, agent=None, audit=False):
        from api.events.triggers import list_triggers_endpoint
        res = asyncio.run(list_triggers_endpoint(agent=agent, scope=None, audit=audit, user=ctx))
        return {t["slug"] for t in res["triggers"]}

    def test_admin_agent_settings_is_user_view(self, temp_db):
        # Admin on the agent's Triggers tab (agent set, no audit): agent-scoped +
        # the admin's OWN — NOT the manager's/viewer's user-scoped triggers.
        self._seed()
        slugs = self._slugs(_admin(), agent=AG, audit=False)
        assert slugs == {"tr-ag"}, slugs  # admin owns none here; only the shared one

    def test_admin_audit_sees_everyone(self, temp_db):
        # Admin Triggers page (audit=true): all triggers across all users.
        self._seed()
        slugs = self._slugs(_admin(), agent=None, audit=True)
        assert slugs == {"tr-ag", "tr-m", "tr-v"}, slugs

    def test_viewer_is_user_view(self, temp_db):
        # A viewer sees agent-scoped + their OWN — never the manager's.
        self._seed()
        slugs = self._slugs(_viewer(), agent=AG, audit=False)
        assert slugs == {"tr-ag", "tr-v"}, slugs

    def test_audit_ignored_for_non_admin(self, temp_db):
        # A non-admin can't escalate to the audit view by passing audit=true.
        self._seed()
        slugs = self._slugs(_viewer(), agent=AG, audit=True)
        assert slugs == {"tr-ag", "tr-v"}, slugs


class TestNotificationsEndpointScoping:
    def _seed(self):
        from storage import notification_store
        agent_store.create_agent(AG, "Shared", created_by="user-admin")
        notification_store.create_notification("n-ag", "b", scope="agent", target=AG, created_by="user-admin")
        notification_store.create_notification("n-m", "b", scope="user", target=M, created_by=M)
        notification_store.create_notification("n-v", "b", scope="user", target=V, created_by=V)

    def _titles(self, ctx, *, agent=None, audit=False):
        from api.notifications.notifications import list_notifications
        res = asyncio.run(list_notifications(
            scope=None, source=None, agent=agent, audit=audit,
            view="definitions", user=ctx, x_agent_name=None,
        ))
        return {n["title"] for n in res["notifications"]}

    def test_admin_agent_settings_is_user_view(self, temp_db):
        self._seed()
        # Admin on the agent's Notifications tab: agent-scoped + own — NOT the
        # manager's/viewer's user-scoped notifications.
        assert self._titles(_admin(), agent=AG, audit=False) == {"n-ag"}

    def test_admin_audit_sees_everyone(self, temp_db):
        self._seed()
        assert self._titles(_admin(), agent=None, audit=True) == {"n-ag", "n-m", "n-v"}

    def test_viewer_is_user_view(self, temp_db):
        self._seed()
        assert self._titles(_viewer(), agent=AG, audit=False) == {"n-ag", "n-v"}

    def test_audit_ignored_for_non_admin(self, temp_db):
        self._seed()
        assert self._titles(_viewer(), agent=AG, audit=True) == {"n-ag", "n-v"}


class TestSessionJwtTaskScoping:
    """H1/H2 (2026-08-11): session JWTs are api-key-shaped but carry a real
    identity — they must get the user-view scope filter AND the
    accessible-agents filter exactly like a cookie caller. Keying those
    filters on ``is_api_key`` let any agent session list OTHER users'
    user-scoped task definitions (prompts included) and other agents'
    tasks. Only the master key is unfiltered."""

    def _session_ctx(self) -> UserContext:
        # A user-backed per-session JWT: is_api_key=True + real sub/roles.
        return UserContext(
            sub=V, email="v@t.com", name="Viewer", role="member",
            agents=[AG], is_api_key=True, session_id="sess-1", agent=AG)

    def _seed_defs(self, monkeypatch):
        from api.tasks import tasks as tasks_mod

        class _Def:
            def __init__(self, id, agent, scope, created_by):
                self.id, self.agent = id, agent
                self.scope, self.created_by = scope, created_by
                self.use_persistent = False
                self.enabled = True

            def model_dump(self):
                return {"id": self.id, "agent": self.agent}

        defs = [
            _Def("t-ag", AG, "agent", "user-admin"),
            _Def("t-m", AG, "user", M),
            _Def("t-v", AG, "user", V),
            _Def("t-other", "other-agent", "agent", "user-admin"),
        ]
        monkeypatch.setattr(
            tasks_mod.scheduler, "get_all_task_definitions", lambda: defs)
        monkeypatch.setattr(
            tasks_mod.scheduler, "get_scheduled_jobs", lambda: [])

    def test_session_jwt_gets_user_view_and_agent_filter(
            self, temp_db, monkeypatch):
        from api.tasks.tasks import list_tasks
        self._seed_defs(monkeypatch)
        res = asyncio.run(list_tasks(
            agent=None, audit=False, user=self._session_ctx()))
        ids = {t["id"] for t in res["tasks"]}
        # Own user-scoped + agent-scoped of ACCESSIBLE agents only — never
        # another user's user-scoped definitions (H1) nor another agent's
        # tasks (H2).
        assert ids == {"t-ag", "t-v"}, ids

    def test_master_key_still_unfiltered(self, temp_db, monkeypatch):
        from api.tasks.tasks import list_tasks
        self._seed_defs(monkeypatch)
        res = asyncio.run(list_tasks(agent=None, audit=False, user=_apikey()))
        assert {t["id"] for t in res["tasks"]} == {
            "t-ag", "t-m", "t-v", "t-other"}


class TestRunsStoreScoping:
    def _seed(self):
        with task_store.get_conn() as conn:
            for rid, by, scope in [
                ("r-ag", None, "agent"),
                ("r-m", M, "user"),
                ("r-v", V, "user"),
            ]:
                conn.execute(
                    "INSERT INTO task_runs (id, task_id, agent, trigger_type, status, "
                    "created_by, scope) VALUES (%s, 't', %s, 'manual', 'completed', %s, %s)",
                    (rid, AG, by, scope),
                )
            conn.commit()

    def test_user_view_excludes_other_users(self, temp_db):
        self._seed()
        # scope_user_sub=V (user-view) → agent-scoped + V's own, not M's.
        ids = {r["id"] for r in task_store.list_runs(scope_user_sub=V)}
        assert ids == {"r-ag", "r-v"}, ids

    def test_audit_sees_all(self, temp_db):
        self._seed()
        # scope_user_sub=None (audit) → every run.
        ids = {r["id"] for r in task_store.list_runs(scope_user_sub=None)}
        assert ids == {"r-ag", "r-m", "r-v"}, ids


class TestSessionJwtTriggerScoping:
    """C3 hardening (2026-08-14): triggers never got the H1/H2 re-key —
    a session JWT could list every user's + every agent's triggers, and
    read/test-fire ANY trigger by id (``_can_view_trigger``'s ``is_api_key``
    blanket). Only the master key is unfiltered now."""

    def _seed(self):
        agent_store.create_agent(AG, "Shared", created_by="user-admin")
        agent_store.create_agent(OTHER, "Other", created_by="user-admin")
        t1 = trigger_store.create_trigger(slug="tr-ag", name="ag", scope="agent", agent=AG, created_by="user-admin")
        t2 = trigger_store.create_trigger(slug="tr-m", name="m", scope="user", agent=AG, created_by=M)
        t3 = trigger_store.create_trigger(slug="tr-v", name="v", scope="user", agent=AG, created_by=V)
        t4 = trigger_store.create_trigger(slug="tr-other", name="o", scope="agent", agent=OTHER, created_by="user-admin")
        return t1, t2, t3, t4

    def _slugs(self, ctx, *, agent=None, audit=False):
        from api.events.triggers import list_triggers_endpoint
        res = asyncio.run(list_triggers_endpoint(agent=agent, scope=None, audit=audit, user=ctx))
        return {t["slug"] for t in res["triggers"]}

    def test_user_session_gets_user_view_and_agent_filter(self, temp_db):
        # Own user-scoped + agent-scoped of ACCESSIBLE agents — never another
        # user's user-scoped triggers nor another agent's.
        self._seed()
        assert self._slugs(_user_session()) == {"tr-ag", "tr-v"}

    def test_nouser_session_sees_own_agent_shared_only(self, temp_db):
        self._seed()
        assert self._slugs(_nouser_session()) == {"tr-ag"}

    def test_master_key_still_unfiltered(self, temp_db):
        self._seed()
        assert self._slugs(_apikey()) == {"tr-ag", "tr-m", "tr-v", "tr-other"}

    def test_get_by_id_denies_foreign(self, temp_db):
        # The IDOR: another user's user-scope + another agent's trigger → 403.
        from api.events.triggers import get_trigger_endpoint
        _t1, t2, t3, t4 = self._seed()
        for foreign in (t2, t4):
            with pytest.raises(HTTPException) as e:
                asyncio.run(get_trigger_endpoint(foreign["id"], user=_user_session()))
            assert e.value.status_code == 403
        # Own trigger stays readable.
        row = asyncio.run(get_trigger_endpoint(t3["id"], user=_user_session()))
        assert row["slug"] == "tr-v"

    def test_fire_by_id_denied_cross_agent(self, temp_db, monkeypatch):
        from api.events import triggers as trig_mod
        _t1, _t2, _t3, t4 = self._seed()

        async def _no_fire(*a, **k):
            raise AssertionError("fire_trigger must not be reached")
        monkeypatch.setattr(trig_mod.trigger_manager, "fire_trigger", _no_fire)

        class _Req:
            async def json(self):
                return {}

        with pytest.raises(HTTPException) as e:
            asyncio.run(trig_mod.fire_test_endpoint(t4["id"], _Req(), user=_nouser_session()))
        assert e.value.status_code == 403

    def test_mutation_authority_nouser_own_agent_only(self, temp_db):
        from api.events.triggers import (
            _can_manage_trigger, _check_trigger_mutation_authority)
        t1, _t2, t3, t4 = self._seed()
        nu = _nouser_session()
        # Own agent's agent-scope: allowed.
        _check_trigger_mutation_authority(t1, nu)
        assert _can_manage_trigger(t1, nu) is True
        # Another agent's agent-scope: denied (the pre-fix hole).
        with pytest.raises(HTTPException):
            _check_trigger_mutation_authority(t4, nu)
        assert _can_manage_trigger(t4, nu) is False
        # User-scope: denied (no identity).
        with pytest.raises(HTTPException):
            _check_trigger_mutation_authority(t3, nu)

    def test_create_pinned_to_own_agent_for_sessions(self, temp_db):
        # Writes never cross agents: an agent session creates triggers only on
        # its own agent — user-backed or not.
        from api.events.triggers import _enforce_create_permission
        agent_store.create_agent(AG, "Shared", created_by="user-admin")
        agent_store.create_agent(OTHER, "Other", created_by="user-admin")
        for ctx in (_user_session(), _nouser_session()):
            with pytest.raises(HTTPException) as e:
                _enforce_create_permission(scope="agent", agent=OTHER, user=ctx)
            assert e.value.status_code == 403
        # Own agent still fine for the no-user session (system-owned row).
        assert _enforce_create_permission(
            scope="agent", agent=AG, user=_nouser_session()) == AG


class TestSessionJwtNotificationScoping:
    """C3 hardening: notifications had the worst leak — a session JWT skipped
    ALL filtering (every user's definitions platform-wide) and the mutation
    gate's ``or user.is_api_key`` blanket let any agent session mutate/fire
    any notification. Plus the cookie-path ``?agent=`` bypass."""

    def _seed(self):
        agent_store.create_agent(AG, "Shared", created_by="user-admin")
        agent_store.create_agent(OTHER, "Other", created_by="user-admin")
        n1 = notification_store.create_notification("n-ag", "b", scope="agent", target=AG, created_by="user-admin")
        n2 = notification_store.create_notification("n-m", "b", scope="user", target=M, created_by=M)
        n3 = notification_store.create_notification("n-v", "b", scope="user", target=V, created_by=V)
        n4 = notification_store.create_notification("n-other", "b", scope="agent", target=OTHER, created_by="user-admin")
        return n1, n2, n3, n4

    def _titles(self, ctx, *, agent=None, audit=False):
        from api.notifications.notifications import list_notifications
        res = asyncio.run(list_notifications(
            scope=None, source=None, agent=agent, audit=audit,
            view="definitions", user=ctx, x_agent_name=None,
        ))
        return {n["title"] for n in res["notifications"]}

    def test_user_session_gets_user_view(self, temp_db):
        self._seed()
        assert self._titles(_user_session()) == {"n-ag", "n-v"}

    def test_nouser_session_sees_own_agent_only(self, temp_db):
        self._seed()
        assert self._titles(_nouser_session()) == {"n-ag"}

    def test_master_key_still_unfiltered(self, temp_db):
        self._seed()
        assert self._titles(_apikey()) == {"n-ag", "n-m", "n-v", "n-other"}

    def test_agent_param_narrows_never_widens(self, temp_db):
        # The cookie-path leak: ``?agent=`` used to REPLACE can_access_agent —
        # a viewer could read an inaccessible agent's notifications by naming it.
        self._seed()
        assert self._titles(_viewer(), agent=OTHER) == {"n-v"}
        assert self._titles(_viewer(), agent=AG) == {"n-ag", "n-v"}

    def test_agent_param_narrows_audit_branch(self, temp_db):
        # ``agent`` used to be silently dropped for the audit/master branch.
        self._seed()
        assert self._titles(_admin(), agent=AG, audit=True) == {"n-ag", "n-m", "n-v"}

    def test_mutation_authority(self, temp_db):
        from api.notifications.notifications import _check_notification_authority
        n1, n2, n3, n4 = self._seed()
        us = _user_session()
        with pytest.raises(HTTPException):
            _check_notification_authority(n2, us)  # another user's row
        _check_notification_authority(n3, us)      # own row
        nu = _nouser_session()
        _check_notification_authority(n1, nu)      # own agent's agent-scope
        for foreign in (n4, n3):
            with pytest.raises(HTTPException):
                _check_notification_authority(foreign, nu)
        _check_notification_authority(n2, _apikey())  # master key: full s2s

    def test_create_scope_gate_pins_sessions(self, temp_db):
        from api.notifications.notifications import _enforce_scope
        agent_store.create_agent(AG, "Shared", created_by="user-admin")
        agent_store.create_agent(OTHER, "Other", created_by="user-admin")
        for ctx in (_user_session(), _nouser_session()):
            with pytest.raises(HTTPException) as e:
                _enforce_scope(ctx, "agent", OTHER)
            assert e.value.status_code == 403
        _enforce_scope(_nouser_session(), "agent", AG)  # own agent OK


class TestSessionJwtSubscriptionScoping:
    """C3 hardening: the subscriptions list keyed its full-table branch on
    ``is_api_key`` — a session JWT read every user's + every agent's rows."""

    def _seed(self):
        agent_store.create_agent(AG, "Shared", created_by="user-admin")
        agent_store.create_agent(OTHER, "Other", created_by="user-admin")
        common = dict(
            mcp_name="gmail-mcp", provider_id="google", account_label="a",
            selected_events=["e"], selected_subevents={}, signing_secret="",
        )
        webhook_subscription_store.create_subscription(
            scope="user", owner=M, agent=None, vendor_target="t-m",
            created_by=M, **common)
        webhook_subscription_store.create_subscription(
            scope="user", owner=V, agent=None, vendor_target="t-v",
            created_by=V, **common)
        webhook_subscription_store.create_subscription(
            scope="service", owner="", agent=AG, vendor_target="t-ag",
            created_by="user-admin", **common)
        webhook_subscription_store.create_subscription(
            scope="service", owner="", agent=OTHER, vendor_target="t-other",
            created_by="user-admin", **common)

    def _targets(self, ctx):
        from api.events.subscriptions import list_subscriptions
        res = asyncio.run(list_subscriptions(
            scope=None, agent=None, mcp_name=None, provider_id=None,
            account_label=None, user=ctx))
        return {r["vendor_target"] for r in res["subscriptions"]}

    def test_user_session_gets_user_view(self, temp_db):
        self._seed()
        assert self._targets(_user_session()) == {"t-v", "t-ag"}

    def test_nouser_session_sees_own_agent_only(self, temp_db):
        self._seed()
        assert self._targets(_nouser_session()) == {"t-ag"}

    def test_master_key_still_unfiltered(self, temp_db):
        self._seed()
        assert self._targets(_apikey()) == {"t-m", "t-v", "t-ag", "t-other"}


class TestRunsPaginationReach:
    """C1 fix: the accessible-agents filter must constrain the SAME query that
    pages and counts — the old Python post-filter returned short pages and an
    inflated ``total`` on cross-agent listings."""

    def _seed(self):
        agent_store.create_agent(AG, "Shared", created_by="user-admin")
        agent_store.create_agent(OTHER, "Other", created_by="user-admin")
        with task_store.get_conn() as conn:
            # OTHER's runs are NEWEST — without the SQL filter they'd occupy
            # the whole first page and the post-filter would empty it.
            rows = [
                ("r-o1", OTHER, "2026-01-03T00:00:00"),
                ("r-o2", OTHER, "2026-01-03T01:00:00"),
                ("r-o3", OTHER, "2026-01-03T02:00:00"),
                ("r-a1", AG, "2026-01-01T00:00:00"),
                ("r-a2", AG, "2026-01-01T01:00:00"),
            ]
            for rid, ag, ts in rows:
                conn.execute(
                    "INSERT INTO task_runs (id, task_id, agent, trigger_type, "
                    "status, created_by, scope, started_at) "
                    "VALUES (%s, 't', %s, 'manual', 'completed', NULL, 'agent', %s)",
                    (rid, ag, ts),
                )
            conn.commit()

    def test_total_and_page_respect_reach(self, temp_db):
        from api.tasks.tasks import list_runs as list_runs_ep
        self._seed()
        res = asyncio.run(list_runs_ep(
            agent=None, status=None, task_id=None, session_id=None,
            created_by=None, audit=False, include_delegates=False,
            limit=2, offset=0, user=_user_session()))
        assert res["total"] == 2, res["total"]
        assert {r["id"] for r in res["runs"]} == {"r-a1", "r-a2"}

    def test_master_key_unconstrained(self, temp_db):
        from api.tasks.tasks import list_runs as list_runs_ep
        self._seed()
        res = asyncio.run(list_runs_ep(
            agent=None, status=None, task_id=None, session_id=None,
            created_by=None, audit=False, include_delegates=False,
            limit=2, offset=0, user=_apikey()))
        assert res["total"] == 5
        assert {r["id"] for r in res["runs"]} == {"r-o3", "r-o2"}


class TestNoUserEdgeReads:
    """A delegation edge grants a NO-USER session read reach into the
    target's AGENT-SCOPE rows (merged observe semantics, 2026-08-15) —
    never user-scope rows, never fire/mutations, and no edge means no
    read reach at all."""

    def _wire(self, edge=True):
        agent_store.create_agent(AG, "Shared", created_by="user-admin")
        agent_store.create_agent(OTHER, "Other", created_by="user-admin")
        if edge:
            agent_store.set_delegation_targets(AG, [OTHER])

    def _trigger_rows(self, ctx):
        from api.events.triggers import list_triggers_endpoint
        res = asyncio.run(list_triggers_endpoint(
            agent=None, scope=None, audit=False, user=ctx))
        return {t["slug"]: t for t in res["triggers"]}

    def test_triggers_list_gains_target_agent_scope_only(self, temp_db):
        self._wire()
        trigger_store.create_trigger(slug="tr-own", name="o", scope="agent", agent=AG, created_by="user-admin")
        trigger_store.create_trigger(slug="tr-obs", name="t", scope="agent", agent=OTHER, created_by="user-admin")
        trigger_store.create_trigger(slug="tr-obs-u", name="tu", scope="user", agent=OTHER, created_by=M)
        rows = self._trigger_rows(_nouser_session())
        assert set(rows) == {"tr-own", "tr-obs"}, set(rows)
        # can_fire must not lie: fire stays own-agent even where view relaxed.
        assert rows["tr-own"]["can_fire"] is True
        assert rows["tr-obs"]["can_fire"] is False

    def test_no_edge_grants_no_reads(self, temp_db):
        self._wire(edge=False)
        trigger_store.create_trigger(slug="tr-obs", name="t", scope="agent", agent=OTHER, created_by="user-admin")
        assert "tr-obs" not in self._trigger_rows(_nouser_session())

    def test_trigger_get_allowed_fire_denied(self, temp_db, monkeypatch):
        from api.events import triggers as trig_mod
        self._wire()
        t = trigger_store.create_trigger(slug="tr-obs", name="t", scope="agent", agent=OTHER, created_by="user-admin")
        row = asyncio.run(trig_mod.get_trigger_endpoint(t["id"], user=_nouser_session()))
        assert row["slug"] == "tr-obs"

        async def _no_fire(*a, **k):
            raise AssertionError("fire_trigger must not be reached")
        monkeypatch.setattr(trig_mod.trigger_manager, "fire_trigger", _no_fire)

        class _Req:
            async def json(self):
                return {}

        with pytest.raises(HTTPException) as e:
            asyncio.run(trig_mod.fire_test_endpoint(t["id"], _Req(), user=_nouser_session()))
        assert e.value.status_code == 403
        assert "read-only" in e.value.detail

    def test_trigger_mutation_still_own_agent(self, temp_db):
        from api.events.triggers import (
            _can_manage_trigger, _check_trigger_mutation_authority)
        self._wire()
        t = trigger_store.create_trigger(slug="tr-obs", name="t", scope="agent", agent=OTHER, created_by="user-admin")
        nu = _nouser_session()
        assert _can_manage_trigger(t, nu) is False
        with pytest.raises(HTTPException):
            _check_trigger_mutation_authority(t, nu)

    def test_notifications_list_gains_target(self, temp_db):
        from api.notifications.notifications import (
            _check_notification_authority, list_notifications)
        self._wire()
        notification_store.create_notification("n-own", "b", scope="agent", target=AG, created_by="user-admin")
        n_obs = notification_store.create_notification("n-obs", "b", scope="agent", target=OTHER, created_by="user-admin")
        notification_store.create_notification("n-u", "b", scope="user", target=M, created_by=M)
        res = asyncio.run(list_notifications(
            scope=None, source=None, agent=None, audit=False,
            view="definitions", user=_nouser_session(), x_agent_name=None))
        assert {n["title"] for n in res["notifications"]} == {"n-own", "n-obs"}
        # Authority untouched: edge-visible rows stay read-only.
        with pytest.raises(HTTPException):
            _check_notification_authority(n_obs, _nouser_session())

    def test_runs_target_agent_scope_only(self, temp_db):
        self._wire()
        with task_store.get_conn() as conn:
            rows = [
                ("r-own-a", AG, "agent", None, "2026-01-01T00:00:00"),
                ("r-own-u", AG, "user", M, "2026-01-01T01:00:00"),
                ("r-obs-a", OTHER, "agent", None, "2026-01-02T00:00:00"),
                ("r-obs-u", OTHER, "user", M, "2026-01-02T01:00:00"),
            ]
            for rid, ag, scope, cb, ts in rows:
                conn.execute(
                    "INSERT INTO task_runs (id, task_id, agent, trigger_type, "
                    "status, created_by, scope, started_at) "
                    "VALUES (%s, 't', %s, 'manual', 'completed', %s, %s, %s)",
                    (rid, ag, cb, scope, ts),
                )
            conn.commit()
        from api.tasks.tasks import list_runs as list_runs_ep
        res = asyncio.run(list_runs_ep(
            agent=None, status=None, task_id=None, session_id=None,
            created_by=None, audit=False, include_delegates=False,
            limit=50, offset=0, user=_nouser_session()))
        # Own agent: BOTH scopes (deliberate — the agent filter is a
        # no-user session's only scope on ITS OWN agent). Edge target:
        # agent-scope only. total counts the same way.
        assert {r["id"] for r in res["runs"]} == {"r-own-a", "r-own-u", "r-obs-a"}
        assert res["total"] == 3

    def test_single_run_access_scope_clamped(self, temp_db):
        from api.tasks.tasks import _check_run_access
        self._wire()
        _check_run_access(
            {"agent": OTHER, "scope": "agent", "created_by": "x"},
            _nouser_session())
        with pytest.raises(HTTPException):
            _check_run_access(
                {"agent": OTHER, "scope": "user", "created_by": "x"},
                _nouser_session())
