"""Agent-settings (user-view) vs admin-audit (full) scoping for tasks / triggers
/ task-history.

The rule: an `agent`-scoped item is shared; a user's user-scoped item is private
to them. The admin audit surface (``audit=true``, admin only) sees every user's
items; everyone else — INCLUDING an admin on an agent's settings tab — sees the
user-view (own user-scoped + agent-scoped).

Run: cd proxy && venv/bin/pytest tests/api/test_audit_view_scoping.py -v
"""

from __future__ import annotations

import asyncio
import sys

from tests._paths import PROXY_DIR
_proxy_root = str(PROXY_DIR)
if _proxy_root not in sys.path:
    sys.path.insert(0, _proxy_root)

from auth.providers import UserContext  # noqa: E402
from storage import agent_store  # noqa: E402
from storage import database as task_store  # noqa: E402
from storage import trigger_store  # noqa: E402

AG = "shared-agent"
M = "user-manager"
V = "user-viewer"


def _admin() -> UserContext:
    return UserContext(sub="user-admin", email="a@t.com", name="Admin", role="admin")


def _viewer() -> UserContext:
    return UserContext(sub=V, email="v@t.com", name="Viewer", role="member", agents=[AG])


def _apikey() -> UserContext:
    return UserContext(sub="api-key", email="", name="", role="admin", is_api_key=True)


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
